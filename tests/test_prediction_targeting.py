from __future__ import annotations

import json
from pathlib import Path

from pipeline.prediction_targeting import build_inputs_manifest, build_inputs_status, find_cached_calendar_entry, load_cached_calendar, select_prediction_target


def test_select_prediction_target_standard_weekend() -> None:
    assert select_prediction_target("standard", []) == "qualifying"
    assert select_prediction_target("standard", ["FP1", "FP2", "Q"]) == "race"


def test_select_prediction_target_sprint_weekend() -> None:
    assert select_prediction_target("sprint", []) == "sprint_qualifying"
    assert select_prediction_target("sprint", ["SQ"]) == "sprint"
    assert select_prediction_target("sprint", ["SQ", "S"]) == "qualifying"
    assert select_prediction_target("sprint", ["SQ", "S", "Q"]) == "race"


def test_build_inputs_manifest_filters_missing_sessions_and_normalizes() -> None:
    manifest = build_inputs_manifest(
        target="race",
        available_sessions=["FP3", "Q"],
        session_weights={
            "race": {
                "history_driver": 0.2,
                "history_team": 0.1,
                "qualifying": 0.4,
                "fp2": 0.1,
                "fp3": 0.1,
                "signals": 0.1,
            }
        },
        active_signal_count=0,
    )
    sources = [row["source"] for row in manifest]
    assert "qualifying" in sources
    assert "fp2" not in sources
    assert "signals" not in sources
    total = sum(float(row["weight"]) for row in manifest)
    assert round(total, 6) == 1.0


def test_cached_calendar_lookup(tmp_path: Path) -> None:
    path = tmp_path / "season_2026.json"
    path.write_text(
        json.dumps(
            [
                {"event_name": "Chinese Grand Prix", "event_format": "sprint"},
                {"event_name": "Japanese Grand Prix", "event_format": "conventional"},
            ]
        ),
        encoding="utf-8",
    )
    calendar = load_cached_calendar(path)
    chinese = find_cached_calendar_entry(calendar, "Chinese Grand Prix")
    japan = find_cached_calendar_entry(calendar, "Japanese Grand Prix")
    assert chinese is not None
    assert chinese["event_format"] == "sprint"
    assert japan is not None
    assert japan["event_format"] == "conventional"


def test_build_inputs_status_marks_used_and_missing() -> None:
    rows = build_inputs_status(
        target="sprint_qualifying",
        available_sessions=[],
        session_weights={
            "sprint_qualifying": {
                "history_driver": 0.5,
                "history_team": 0.3,
                "fp1": 0.2,
                "signals": 0.0,
            }
        },
        active_signal_count=0,
    )
    by_source = {row["source"]: row for row in rows}
    assert by_source["history_driver"]["status"] == "used"
    assert by_source["history_team"]["status"] == "used"
    assert by_source["fp1"]["status"] == "missing"
    assert by_source["signals"]["status"] == "available_zero_weight"
