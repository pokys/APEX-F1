from __future__ import annotations

import json
from pathlib import Path

from pipeline.prediction_targeting import build_inputs_manifest, build_inputs_status, compute_data_freshness, compute_weekend_form, find_cached_calendar_entry, latest_completed_event, load_cached_calendar, load_session_weights, select_prediction_target


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


def test_config_session_weights_prioritize_current_inputs() -> None:
    weights = load_session_weights(Path("config/session_weights.json"))

    current_inputs = {
        "qualifying": {"fp1", "fp2", "fp3", "signals"},
        "race": {"qualifying", "fp2", "fp3", "signals"},
        "sprint_qualifying": {"fp1", "signals"},
        "sprint": {"sprint_qualifying", "fp1", "signals"},
    }

    for target, sources in current_inputs.items():
        target_weights = weights[target]
        history_weight = target_weights["history_driver"] + target_weights["history_team"]
        current_weight = sum(target_weights[source] for source in sources)
        assert current_weight > history_weight


def test_compute_weekend_form_blends_history_baseline_with_sessions() -> None:
    event = {
        "sessions": [
            {
                "session_code": "FP1",
                "results": [
                    {"position": 1, "abbreviation": "VER"},
                    {"position": 2, "abbreviation": "NOR"},
                    {"position": 3, "abbreviation": "LEC"},
                ],
            }
        ]
    }
    manifest = [
        {"source": "history_driver", "source_key": "history_driver", "weight": 0.4},
        {"source": "history_team", "source_key": "history_team", "weight": 0.2},
        {"source": "fp1", "source_key": "FP1", "weight": 0.4},
    ]

    form = compute_weekend_form("VER", event, manifest)

    assert form["sources"] == [{"session": "FP1", "weight": 0.4, "score": 1.0}]
    assert round(form["delta"], 6) == 2.0


def test_latest_completed_event_picks_most_recent_with_results() -> None:
    snapshot = {
        "events": [
            {
                "event_name": "Australian Grand Prix",
                "event_date": "2026-03-08",
                "sessions": [{"session_code": "R", "results": [{"position": 1}]}],
            },
            {
                "event_name": "Chinese Grand Prix",
                "event_date": "2026-03-15",
                "sessions": [{"session_code": "R", "results": [{"position": 1}]}],
            },
            # Future placeholder with no results - must be ignored.
            {
                "event_name": "Bahrain Grand Prix",
                "event_date": "2026-04-12",
                "sessions": [{"session_code": "R", "results": []}],
            },
        ]
    }
    name, when = latest_completed_event(snapshot)
    assert name == "Chinese Grand Prix"
    assert when.isoformat() == "2026-03-15"


def test_compute_data_freshness_flags_long_gap_as_stale() -> None:
    snapshot = {
        "events": [
            {
                "event_name": "Japanese Grand Prix",
                "event_date": "2026-03-29",
                "sessions": [{"session_code": "R", "results": [{"position": 1}]}],
            },
        ]
    }
    freshness = compute_data_freshness(
        snapshot,
        race_date="2026-05-03",
        generated_at="2026-05-01T09:00:00Z",
        stale_threshold_days=21,
    )
    assert freshness["latest_completed_event"] == "Japanese Grand Prix"
    assert freshness["latest_completed_date"] == "2026-03-29"
    assert freshness["days_since_latest_event"] == 33
    assert freshness["days_until_next_race"] == 2
    assert freshness["is_stale"] is True


def test_compute_data_freshness_recent_event_not_stale() -> None:
    snapshot = {
        "events": [
            {
                "event_name": "Miami Grand Prix",
                "event_date": "2026-05-03",
                "sessions": [{"session_code": "R", "results": [{"position": 1}]}],
            },
        ]
    }
    freshness = compute_data_freshness(
        snapshot,
        race_date="2026-05-24",
        generated_at="2026-05-08T09:00:00Z",
        stale_threshold_days=21,
    )
    assert freshness["days_since_latest_event"] == 5
    assert freshness["is_stale"] is False


def test_compute_data_freshness_no_results_is_stale() -> None:
    freshness = compute_data_freshness(
        {"events": []},
        race_date="2026-05-03",
        generated_at="2026-05-01T09:00:00Z",
        stale_threshold_days=21,
    )
    assert freshness["latest_completed_event"] is None
    assert freshness["latest_completed_date"] is None
    assert freshness["days_since_latest_event"] is None
    assert freshness["is_stale"] is True
