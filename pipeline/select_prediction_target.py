#!/usr/bin/env python3
"""
Automatically select what the pipeline should currently predict.

Updates config/race_config.json with:
- prediction_target
- prediction_target_label
- target_session_code
- target_output_type
- weekend_format
- available_sessions
- inputs_used
- fixed_grid / grid_source when applicable
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.prediction_targeting import (  # noqa: E402
    DEFAULT_SESSION_COMPLETION_BUFFER_MINUTES,
    TARGET_LABEL,
    TARGET_OUTPUT_TYPE,
    TARGET_SESSION_CODE,
    available_sessions_for_event,
    build_inputs_manifest,
    build_inputs_status,
    extract_fixed_grid_from_event,
    find_cached_calendar_entry,
    find_calendar_entry,
    find_event,
    load_cached_calendar,
    load_json,
    load_session_weights,
    normalize_weekend_format,
    select_prediction_target,
    sessions_completed_by_calendar,
    signal_count,
)


LOGGER = logging.getLogger("select_prediction_target")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select the current automatic prediction target.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config JSON path.")
    parser.add_argument("--raw-dir", default="data/raw/fastf1", help="FastF1 raw snapshot directory.")
    parser.add_argument("--calendar-cache-dir", default="data/raw/calendars", help="Normalized calendar cache directory.")
    parser.add_argument("--session-weights", default="config/session_weights.json", help="Session weights config path.")
    parser.add_argument("--signals-dir", default="knowledge/processed", help="Processed signal directory.")
    parser.add_argument(
        "--reference-time",
        default=None,
        help="UTC ISO datetime used as 'now' for calendar-based session completion "
        "(default: current UTC time). Sessions scheduled more than --calendar-completion-buffer "
        "minutes before this point are treated as completed even if FastF1 hasn't ingested results yet.",
    )
    parser.add_argument(
        "--calendar-completion-buffer-minutes",
        type=float,
        default=DEFAULT_SESSION_COMPLETION_BUFFER_MINUTES,
        help="Minutes after a session's scheduled start before we consider it finished by calendar.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    config_path = Path(args.race_config)
    if not config_path.exists():
        LOGGER.error("Missing race config: %s", config_path)
        return 1

    try:
        config = load_json(config_path)
        if not isinstance(config, dict):
            raise ValueError("Race config must be a JSON object.")

        season = int(config.get("season"))
        race_name = str(config.get("race") or "").strip()
        if not race_name:
            raise ValueError("Race config missing 'race'.")

        snapshot_path = Path(args.raw_dir) / f"season_{season}.json"
        snapshot = load_json(snapshot_path)
        if not isinstance(snapshot, dict):
            raise ValueError("FastF1 snapshot must be a JSON object.")

        event = find_event(snapshot, race_name)
        calendar_entry = find_calendar_entry(snapshot, race_name)
        cached_calendar = load_cached_calendar(Path(args.calendar_cache_dir) / f"season_{season}.json")
        cached_entry = find_cached_calendar_entry(cached_calendar, race_name)
        if event is None and calendar_entry is None:
            LOGGER.warning(
                "Race '%s' not found in %s. Falling back to config-only target selection.",
                race_name,
                snapshot_path,
            )

        ingested_sessions = available_sessions_for_event(event) if event is not None else []
        event_format = ""
        country = ""
        for source in (event, calendar_entry, cached_entry):
            if not isinstance(source, dict):
                continue
            if not event_format:
                event_format = str(source.get("event_format") or "")
            if not country:
                country = str(source.get("country") or "").strip()
            if event_format and country:
                break
        if not country:
            country = str(config.get("country") or config.get("location") or "").strip()

        # Calendar fallback: if FastF1 hasn't yet ingested a session whose
        # scheduled start time is in the past, we still advance the
        # prediction target. The race weekend has clear time-based stages
        # (FP1 -> SQ -> S -> Q -> R) and the user must not be stuck on
        # "predicting SQ" hours after SQ has actually run just because the
        # upstream timing API has a multi-hour delay or got renumbered.
        sessions_schedule: dict[str, Any] | None = None
        for source in (calendar_entry, cached_entry):
            if isinstance(source, dict) and isinstance(source.get("sessions_schedule"), dict):
                sessions_schedule = source.get("sessions_schedule")
                break
        if sessions_schedule is None and isinstance(config.get("sessions_schedule"), dict):
            sessions_schedule = config.get("sessions_schedule")

        if args.reference_time:
            reference_time = args.reference_time
        else:
            reference_time = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        # Best-guess weekend format for completion calibration. Real weekend
        # format gets recomputed below from the merged available_sessions
        # list; this preliminary one is just used to look up local end-hours.
        provisional_format = "sprint" if "sprint" in event_format.lower() else "conventional"
        calendar_completed = sessions_completed_by_calendar(
            sessions_schedule,
            reference_time=reference_time,
            buffer_minutes=args.calendar_completion_buffer_minutes,
            country=country,
            weekend_format=provisional_format,
        )

        seen: set[str] = set()
        available_sessions: list[str] = []
        for code in ingested_sessions + calendar_completed:
            up = str(code).strip().upper()
            if up and up not in seen:
                seen.add(up)
                available_sessions.append(up)
        # Fall back to whatever the config remembered if both sources are empty.
        if not available_sessions:
            available_sessions = [
                str(c).strip().upper()
                for c in (config.get("available_sessions") or [])
                if str(c).strip()
            ]

        weekend_format = normalize_weekend_format(event_format, available_sessions)
        target = select_prediction_target(weekend_format, available_sessions)
        target_code = TARGET_SESSION_CODE[target]
        target_label = TARGET_LABEL[target]
        target_output_type = TARGET_OUTPUT_TYPE[target]

        weights = load_session_weights(Path(args.session_weights))
        active_signal_count = signal_count(Path(args.signals_dir))
        inputs_used = build_inputs_manifest(
            target=target,
            available_sessions=available_sessions,
            session_weights=weights,
            active_signal_count=active_signal_count,
        )
        inputs_status = build_inputs_status(
            target=target,
            available_sessions=available_sessions,
            session_weights=weights,
            active_signal_count=active_signal_count,
        )

        config["available_sessions"] = available_sessions
        config["available_sessions_ingested"] = ingested_sessions
        config["available_sessions_by_calendar"] = calendar_completed
        config["weekend_format"] = weekend_format
        config["prediction_target"] = target
        config["prediction_target_label"] = target_label
        config["target_session_code"] = target_code
        config["target_output_type"] = target_output_type
        config["inputs_used"] = inputs_used
        config["inputs_status"] = inputs_status
        config["signal_count"] = active_signal_count

        if target == "race":
            fixed_grid = extract_fixed_grid_from_event(event, "Q") if event is not None else None
            if fixed_grid:
                config["fixed_grid"] = fixed_grid
                config["grid_source"] = "qualifying"
            else:
                config.pop("fixed_grid", None)
                config["grid_source"] = "simulation"
        elif target == "sprint":
            fixed_grid = extract_fixed_grid_from_event(event, "SQ") if event is not None else None
            if fixed_grid:
                config["fixed_grid"] = fixed_grid
                config["grid_source"] = "sprint_qualifying"
            else:
                config.pop("fixed_grid", None)
                config["grid_source"] = "simulation"
        else:
            config.pop("fixed_grid", None)
            config["grid_source"] = "simulation"

        config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.error("select_prediction_target failed: %s", exc)
        return 1

    LOGGER.info(
        "Selected prediction target: %s (%s), weekend=%s, sessions=%s",
        target,
        target_code,
        weekend_format,
        ",".join(available_sessions) or "none",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
