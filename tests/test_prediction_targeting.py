from __future__ import annotations

from pipeline.prediction_targeting import build_inputs_manifest, select_prediction_target


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
