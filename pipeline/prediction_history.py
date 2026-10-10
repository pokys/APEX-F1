#!/usr/bin/env python3
"""
History of headline probabilities per weekend phase, for "what changed".

A phase is one prediction target together with the set of sessions whose
data are already in the snapshot. The pipeline recomputes every hour, but
inside a phase the inputs barely move, so each phase keeps only its latest
prediction (overwritten on every run). When a new session is ingested a new
phase starts. The dashboard compares the current phase with the previous
phase of the same target, i.e. with the prediction as it stood right before
the most recent session, not with the previous hourly run. That way a
change cannot be missed between two visits.
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

from pipeline.prediction_targeting import SESSION_ORDER, load_json  # noqa: E402

LOGGER = logging.getLogger("prediction_history")

QUALIFYING_TARGETS = {"qualifying", "sprint_qualifying"}
KEEP_EVENTS = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record headline probabilities per weekend phase.")
    parser.add_argument("--prediction-dry", default="outputs/prediction_dry.json")
    parser.add_argument("--prediction-wet", default="outputs/prediction_wet.json")
    parser.add_argument("--race-config", default="config/race_config.json")
    parser.add_argument("--history", default="outputs/prediction_history.json")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def headline_probabilities(prediction: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(prediction, dict):
        return {}
    key = "pole_probability" if str(prediction.get("prediction_target") or "") in QUALIFYING_TARGETS else "win_probability"
    out: dict[str, float] = {}
    for row in prediction.get("drivers") or []:
        if isinstance(row, dict) and row.get("name"):
            try:
                out[str(row["name"])] = round(float(row.get(key) or 0.0), 6)
            except (TypeError, ValueError):
                continue
    return out


def ordered_sessions(sessions: Any) -> list[str]:
    codes = {str(code).strip().upper() for code in sessions or [] if str(code).strip()}
    return sorted(codes, key=lambda code: (SESSION_ORDER.get(code, 99), code))


def event_key(race_config: dict[str, Any]) -> str:
    return f"{race_config.get('season')}-{int(race_config.get('next_round') or 0):02d}"


def is_regression(phase: dict[str, Any], earlier: list[dict[str, Any]]) -> bool:
    """A phase whose sessions are a strict subset of an earlier phase of the
    same target only appears when a run lost already ingested data (a live
    source outage); it is not a real phase of the weekend."""
    sessions = set(phase.get("sessions") or [])
    return any(
        p.get("target") == phase.get("target") and sessions < set(p.get("sessions") or [])
        for p in earlier
    )


def clean_phases(phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for phase in phases:
        if is_regression(phase, out):
            continue
        if out and out[-1].get("target") == phase.get("target") and out[-1].get("sessions") == phase.get("sessions"):
            out[-1] = phase  # the same phase again (after a dropped regression)
        else:
            out.append(phase)
    return out


def record_phase(
    history: dict[str, Any],
    race_config: dict[str, Any],
    prediction_dry: dict[str, Any],
    prediction_wet: dict[str, Any] | None,
) -> dict[str, Any]:
    """Add or overwrite the current phase. Returns the updated history."""
    events = history.setdefault("events", {})
    key = event_key(race_config)
    event = events.setdefault(key, {"race": race_config.get("race"), "phases": []})
    target = str(prediction_dry.get("prediction_target") or race_config.get("prediction_target") or "race")
    sessions = ordered_sessions(race_config.get("available_sessions_ingested"))
    phase = {
        "target": target,
        "sessions": sessions,
        "generated_at": prediction_dry.get("generated_at"),
        "dry": headline_probabilities(prediction_dry),
        "wet": headline_probabilities(prediction_wet),
    }
    phases = clean_phases(event["phases"])
    if is_regression(phase, phases):
        LOGGER.warning("Not recording phase %s %s: earlier runs had more sessions.", target, sessions)
    elif phases and phases[-1].get("target") == target and phases[-1].get("sessions") == sessions:
        phases[-1] = phase
    else:
        phases.append(phase)
    event["phases"] = phases
    for old in sorted(events)[:-KEEP_EVENTS]:
        events.pop(old, None)
    return history


def phase_changes(history: dict[str, Any] | None, race_config: dict[str, Any], scenario: str = "dry") -> dict[str, Any] | None:
    """Change of the headline probability against the previous phase of the
    same target. None when there is no earlier phase to compare with."""
    if not isinstance(history, dict):
        return None
    event = (history.get("events") or {}).get(event_key(race_config))
    phases = clean_phases(event.get("phases") or []) if isinstance(event, dict) else None
    if not phases or len(phases) < 2:
        return None
    current = phases[-1]
    current_sessions = set(current.get("sessions") or [])
    reference = next(
        (
            p
            for p in reversed(phases[:-1])
            if p.get("target") == current.get("target") and set(p.get("sessions") or []) < current_sessions
        ),
        None,
    )
    if reference is None:
        return None
    now = current.get(scenario) or {}
    before = reference.get(scenario) or {}
    new_sessions = [code for code in current.get("sessions") or [] if code not in (reference.get("sessions") or [])]
    deltas = {name: round(prob - before.get(name, 0.0), 6) for name, prob in now.items()}
    return {
        "reference_sessions": reference.get("sessions") or [],
        "new_sessions": new_sessions,
        "label": ("before " + "+".join(new_sessions)) if new_sessions else "previous phase",
        "deltas": deltas,
        "before": {name: round(before.get(name, 0.0), 6) for name in now},
    }


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")
    try:
        race_config = load_json(Path(args.race_config))
        dry = load_json(Path(args.prediction_dry))
        wet_path = Path(args.prediction_wet)
        wet = load_json(wet_path) if wet_path.exists() else None
        history_path = Path(args.history)
        history = load_json(history_path) if history_path.exists() else {}
        history = record_phase(history if isinstance(history, dict) else {}, race_config, dry, wet)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps(history, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    except Exception as exc:
        # The history only feeds the dashboard; never block the pipeline.
        LOGGER.warning("prediction_history skipped: %s", exc)
        return 0
    LOGGER.info("Recorded prediction phase in %s", args.history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
