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
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.prediction_targeting import (  # noqa: E402
    TARGET_LABEL,
    TARGET_OUTPUT_TYPE,
    TARGET_SESSION_CODE,
    available_sessions_for_event,
    build_inputs_manifest,
    extract_fixed_grid_from_event,
    find_calendar_entry,
    find_event,
    load_json,
    load_session_weights,
    normalize_weekend_format,
    select_prediction_target,
    signal_count,
)


LOGGER = logging.getLogger("select_prediction_target")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select the current automatic prediction target.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config JSON path.")
    parser.add_argument("--raw-dir", default="data/raw/fastf1", help="FastF1 raw snapshot directory.")
    parser.add_argument("--session-weights", default="config/session_weights.json", help="Session weights config path.")
    parser.add_argument("--signals-dir", default="knowledge/processed", help="Processed signal directory.")
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
        if event is None and calendar_entry is None:
            LOGGER.warning(
                "Race '%s' not found in %s. Falling back to config-only target selection.",
                race_name,
                snapshot_path,
            )

        available_sessions = available_sessions_for_event(event) if event is not None else list(config.get("available_sessions") or [])
        event_format = ""
        if event is not None:
            event_format = str(event.get("event_format") or "")
        elif calendar_entry is not None:
            event_format = str(calendar_entry.get("event_format") or "")
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

        config["available_sessions"] = available_sessions
        config["weekend_format"] = weekend_format
        config["prediction_target"] = target
        config["prediction_target_label"] = target_label
        config["target_session_code"] = target_code
        config["target_output_type"] = target_output_type
        config["inputs_used"] = inputs_used
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
