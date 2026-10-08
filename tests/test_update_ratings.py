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


def _team_row(team, q_avg, race_avg, **extra):
    row = {"team": team, "qualifying_avg_position": q_avg, "race_avg_position": race_avg, "starts": 32, "dnf_rate": 0.1}
    row.update(extra)
    return row


def test_race_position_gain_rewards_gaining_places_against_field_trend() -> None:
    from pipeline.update_ratings import compute_strategy_scores, compute_team_ratings

    features = {
        "teams": [
            _team_row("Front", 2.0, 3.0),
            _team_row("Mid", 10.0, 10.0),
            _team_row("Charger", 15.0, 11.0),
            _team_row("Back", 18.0, 18.5),
        ]
    }
    teams = {row["team"]: row for row in compute_team_ratings(features, ["Front", "Mid", "Charger", "Back"])["teams"]}
    strategy = {row["team"]: row for row in compute_strategy_scores(features, {}, ["Front", "Mid", "Charger", "Back"])["teams"]}

    # Gaining places is positive in both team and strategy ratings.
    assert teams["Charger"]["components"]["race_position_gain"] > 50.0
    assert strategy["Charger"]["components"]["race_position_gain"] > 50.0
    assert teams["Charger"]["components"]["race_position_gain"] > teams["Back"]["components"]["race_position_gain"]
    # A front-runner losing one place is not punished like a backmarker.
    assert teams["Front"]["components"]["race_position_gain"] >= teams["Back"]["components"]["race_position_gain"]
    assert "sector_dominance" not in teams["Front"]["components"]
    assert "pit_stop_performance" not in strategy["Front"]["components"]


def test_rating_components_have_real_inputs_and_stay_in_range() -> None:
    from pipeline.update_ratings import compute_strategy_scores, compute_team_ratings

    features = {"teams": [_team_row("Mercedes", 4.5, 5.5, points_total=496.0), _team_row("Williams", 15.6, 16.4, points_total=12.0)]}
    teams = compute_team_ratings(features, ["Mercedes", "Williams"])["teams"]
    strategy = compute_strategy_scores(features, {}, ["Mercedes", "Williams"])["teams"]
    for row in teams + strategy:
        for name in ("upgrades_impact", "weekend_pace_proxy", "sprint_pace_proxy", "safety_car_reactions", "strategic_success_history"):
            assert name not in row["components"], (row["team"], name)
        for value in row["components"].values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                assert 0.0 <= value <= 100.0


def test_sprint_execution_is_neutral_without_sprint_qualifying_data() -> None:
    from pipeline.update_ratings import compute_strategy_scores

    features = {"teams": [_team_row("Top", 3.0, 3.0, sprint_avg_position=3.0), _team_row("Low", 18.0, 18.0, sprint_avg_position=18.0)]}
    rows = {row["team"]: row for row in compute_strategy_scores(features, {}, ["Top", "Low"])["teams"]}
    assert rows["Top"]["components"]["sprint_execution"] == 50.0
    assert rows["Low"]["components"]["sprint_execution"] == 50.0

    features["teams"][0]["sprint_qualifying_avg_position"] = 5.0
    rows = {row["team"]: row for row in compute_strategy_scores(features, {}, ["Top", "Low"])["teams"]}
    assert rows["Top"]["components"]["sprint_execution"] > 50.0


def test_new_driver_starts_below_teammate_and_field_median() -> None:
    def driver(name, team, q_avg, race_avg):
        return {"driver": name, "team": team, "qualifying_avg_position": q_avg, "race_avg_position": race_avg, "starts": 10, "dnf_rate": 0.1}

    features = {
        "drivers": [
            driver("AAA", "Fast", 2.0, 2.0),
            driver("BBB", "Fast", 3.0, 3.0),
            driver("CCC", "Mid", 9.0, 9.0),
            driver("DDD", "Mid", 11.0, 11.0),
            driver("EEE", "Slow", 18.0, 18.0),
        ]
    }
    active = {"AAA": "Fast", "NEW": "Fast", "CCC": "Mid", "DDD": "Mid", "EEE": "Slow", "ROK": "Slow"}
    rows = {row["driver"]: row for row in compute_driver_ratings(features, wet_by_team={}, active_drivers=active)["drivers"]}

    assert rows["NEW"]["qualifying_rating"] < rows["AAA"]["qualifying_rating"]
    assert rows["ROK"]["qualifying_rating"] < rows["EEE"]["qualifying_rating"]
    median_q = sorted(row["qualifying_rating"] for name, row in rows.items() if name not in {"NEW", "ROK"})[2]
    assert rows["ROK"]["qualifying_rating"] < median_q
    assert rows["NEW"]["components"] == {"new_driver_baseline": True}


def test_dnf_probability_follows_observed_rate_with_shrinkage() -> None:
    from pipeline.update_ratings import compute_reliability_scores

    features = {
        "teams": [
            {"team": "Fragile", "starts": 32, "dnf_rate": 0.5},
            {"team": "Solid", "starts": 32, "dnf_rate": 0.125},
            {"team": "Steady", "starts": 32, "dnf_rate": 0.1},
            {"team": "Calm", "starts": 32, "dnf_rate": 0.1},
            {"team": "Rookie", "starts": 2, "dnf_rate": 1.0},
        ]
    }
    names = ["Fragile", "Solid", "Steady", "Calm", "Rookie"]
    rows = {row["team"]: row for row in compute_reliability_scores(features, {}, names)["teams"]}
    assert 0.4 < rows["Fragile"]["dnf_probability"] <= 0.45
    assert 0.12 < rows["Solid"]["dnf_probability"] < 0.2
    # Two DNFs in two starts are shrunk heavily towards the field rate.
    assert rows["Rookie"]["dnf_probability"] < 0.45
    assert rows["Fragile"]["dnf_probability"] > rows["Solid"]["dnf_probability"]


def test_simulation_uses_dnf_probability() -> None:
    import random

    from pipeline.simulate_race import simulate_single_race

    entries = [
        {"name": "AAA", "team": "A", "driver_rating": 60.0, "team_rating": 60.0, "strategy_score": 50.0, "reliability_score": 99.0, "dnf_probability": 1.0},
        {"name": "BBB", "team": "B", "driver_rating": 40.0, "team_rating": 40.0, "strategy_score": 50.0, "reliability_score": 99.0, "dnf_probability": 0.0},
    ]
    result = simulate_single_race(entries, ["AAA", "BBB"], random.Random(1), 0.0, 0.5, 0.0, 0.5, 3.0)
    assert result == {"BBB": 1, "AAA": 3}
