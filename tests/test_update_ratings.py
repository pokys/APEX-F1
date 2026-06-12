from __future__ import annotations

import json
from pathlib import Path

from pipeline.update_ratings import (
    choose_features_file,
    aggregate_optional_signal_indexes,
    compute_driver_ratings,
    DEFAULT_SIGNAL_GUARDRAILS,
    current_season_blend_weight,
    load_current_entry_list,
)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_choose_features_file_falls_back_to_previous_season(tmp_path: Path) -> None:
    root = tmp_path / "processed"
    root.mkdir(parents=True)

    write_json(
        root / "features_season_2025.json",
        {"season": 2025, "drivers": [{"driver": "AAA", "team": "X"}], "teams": [{"team": "X"}]},
    )
    write_json(
        root / "features_season_2026.json",
        {"season": 2026, "drivers": [], "teams": []},
    )

    chosen = choose_features_file(root, season=2026)
    assert chosen.name == "features_season_2025.json"


def test_penalty_index_is_capped_by_guardrails() -> None:
    guardrails = json.loads(json.dumps(DEFAULT_SIGNAL_GUARDRAILS))
    signals = [
        {
            "team": "Aston Martin",
            "new_component_penalty": 0.95,
            "source_confidence": 0.9,
            "source_name": "autosport",
            "article_hash": "h1",
        }
    ]
    _, _, penalties = aggregate_optional_signal_indexes(signals, guardrails=guardrails)
    assert penalties["aston-martin"] <= guardrails["penalty_index_cap"]


def test_current_season_blend_weight_favors_current_season_earlier() -> None:
    assert current_season_blend_weight(1) == 0.45
    assert current_season_blend_weight(2) == 0.60
    assert current_season_blend_weight(3) == 0.75
    assert current_season_blend_weight(4) == 0.90
    assert current_season_blend_weight(5) == 1.0


def test_current_season_blend_weight_accepts_fractional_effective_starts() -> None:
    # Two recency-weighted races (effective starts ~ 1.8) should land in the
    # 1<x<=2 bucket. This mirrors the ESS produced by build_features when
    # races have been skipped or deprioritised.
    assert current_season_blend_weight(1.8) == 0.60
    assert current_season_blend_weight(0.5) == 0.45
    assert current_season_blend_weight(0.0) == 0.0


def test_compute_driver_ratings_softens_teammate_penalty_on_small_samples() -> None:
    features = {
        "drivers": [
            {
                "driver": "ANT",
                "team": "Mercedes",
                "starts": 2,
                "race_avg_position": 1.5,
                "race_form_last3": 1.5,
                "qualifying_avg_position": 1.5,
                "qualifying_phase_depth": 1.0,
                "dnf_rate": 0.0,
            },
            {
                "driver": "RUS",
                "team": "Mercedes",
                "starts": 2,
                "race_avg_position": 1.5,
                "race_form_last3": 1.5,
                "qualifying_avg_position": 1.5,
                "qualifying_phase_depth": 1.0,
                "dnf_rate": 0.0,
            },
        ]
    }

    ratings = compute_driver_ratings(features, wet_by_team={}, active_drivers={"ANT": "Mercedes", "RUS": "Mercedes"})
    by_driver = {row["driver"]: row for row in ratings["drivers"]}

    assert by_driver["ANT"]["driver_rating"] > 55.0
    assert by_driver["RUS"]["driver_rating"] > 55.0
    assert by_driver["ANT"]["qualifying_rating"] > 55.0
    assert by_driver["ANT"]["race_rating"] > 55.0
    assert by_driver["ANT"]["components"]["teammate_delta_performance"] == 50.0


def test_compute_driver_ratings_uses_timing_gap_components() -> None:
    features = {
        "drivers": [
            {
                "driver": "AAA",
                "team": "A",
                "starts": 3,
                "race_avg_position": 1.5,
                "race_gap_to_winner_seconds": 2.0,
                "race_form_last3": 1.5,
                "qualifying_avg_position": 1.5,
                "qualifying_gap_to_best_ms": 50.0,
                "teammate_qualifying_gap_ms": 0.0,
                "qualifying_phase_depth": 1.0,
                "dnf_rate": 0.0,
            },
            {
                "driver": "BBB",
                "team": "B",
                "starts": 3,
                "race_avg_position": 12.0,
                "race_gap_to_winner_seconds": 60.0,
                "race_form_last3": 12.0,
                "qualifying_avg_position": 12.0,
                "qualifying_gap_to_best_ms": 1800.0,
                "teammate_qualifying_gap_ms": 0.0,
                "qualifying_phase_depth": 0.33,
                "dnf_rate": 0.0,
            },
        ]
    }

    ratings = compute_driver_ratings(features, wet_by_team={}, active_drivers={"AAA": "A", "BBB": "B"})
    by_driver = {row["driver"]: row for row in ratings["drivers"]}

    assert by_driver["AAA"]["qualifying_rating"] > by_driver["BBB"]["qualifying_rating"]
    assert by_driver["AAA"]["race_rating"] > by_driver["BBB"]["race_rating"]


def test_load_current_entry_list_uses_latest_competitive_session(tmp_path: Path) -> None:
    raw_dir = tmp_path / "fastf1"
    raw_dir.mkdir()
    write_json(
        raw_dir / "season_2026.json",
        {
            "season": 2026,
            "events": [
                {
                    "round": 1,
                    "event_date": "2026-03-08",
                    "sessions": [
                        {
                            "session_code": "FP1",
                            "results": [
                                {"abbreviation": "RES", "team_name": "Mercedes"},
                                {"abbreviation": "RUS", "team_name": "Mercedes"},
                            ],
                        },
                        {
                            "session_code": "Q",
                            "results": [
                                {"abbreviation": "RUS", "team_name": "Mercedes"},
                                {"abbreviation": "ANT", "team_name": "Mercedes"},
                            ],
                        },
                    ],
                }
            ],
        },
    )

    drivers, teams = load_current_entry_list(raw_dir, 2026)

    assert drivers == {"ANT": "Mercedes", "RUS": "Mercedes"}
    assert teams == ["Mercedes"]


def test_load_current_entry_list_falls_back_to_practice_when_no_competitive_session(tmp_path: Path) -> None:
    raw_dir = tmp_path / "fastf1"
    raw_dir.mkdir()
    write_json(
        raw_dir / "season_2026.json",
        {
            "season": 2026,
            "events": [
                {
                    "round": 1,
                    "event_date": "2026-03-08",
                    "sessions": [
                        {
                            "session_code": "FP1",
                            "results": [
                                {"abbreviation": "RUS", "team_name": "Mercedes"},
                                {"abbreviation": "ANT", "team_name": "Mercedes"},
                            ],
                        }
                    ],
                }
            ],
        },
    )

    drivers, teams = load_current_entry_list(raw_dir, 2026)

    assert drivers == {"ANT": "Mercedes", "RUS": "Mercedes"}
    assert teams == ["Mercedes"]
