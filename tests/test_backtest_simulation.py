from __future__ import annotations

from pipeline.backtest_simulation import event_chronology_key


def test_backtest_orders_cancelled_or_skipped_rounds_by_event_date() -> None:
    events = [
        {"round": 5, "event_name": "Later GP", "event_date": "2026-05-24"},
        {"round": 6, "event_name": "Earlier GP", "event_date": "2026-05-03"},
    ]

    ordered = sorted(events, key=event_chronology_key)

    assert [event["event_name"] for event in ordered] == ["Earlier GP", "Later GP"]


import math

from pipeline.backtest_simulation import (
    blend_report,
    build_event_config,
    choose_blend,
    repeat_baseline,
    search_noise_scale,
)
from pipeline.prediction_targeting import championship_points_before, standings_weights


def test_noise_scale_search_extends_past_grid_boundary() -> None:
    calls: list[float] = []

    def loss(scale: float) -> float:
        calls.append(scale)
        return abs(scale - 7.0)

    best, best_loss, losses = search_noise_scale(loss, initial=(1.0, 2.0, 4.0))
    assert best == 6.0
    assert max(losses) > best  # the edge was pushed beyond the optimum
    assert best_loss == 1.0


def test_choose_blend_reports_leave_one_out() -> None:
    model = {"A": 0.9, "B": 0.1}
    base = {"A": 0.5, "B": 0.5}
    rows = [(model, base, "A")] * 9 + [(model, base, "B")]
    weight, in_sample, loo = choose_blend(rows)
    assert 0.0 <= weight <= 0.9
    assert loo >= in_sample - 1e-12
    report = blend_report(rows, fixed=0.5)
    assert report["weight"] == 0.5
    expected = (9 * -math.log(0.7) - math.log(0.3)) / 10
    assert abs(report["loss"] - expected) < 1e-6


def test_baselines_are_distributions_and_use_only_prior_points() -> None:
    names = ["AAA", "BBB", "CCC"]
    assert abs(sum(repeat_baseline(names, "BBB").values()) - 1.0) < 1e-12
    assert repeat_baseline(names, "BBB")["BBB"] == 0.3
    snapshot = {
        "events": [
            {"event_date": "2026-03-01", "sessions": [{"session_code": "R", "results": [{"abbreviation": "CCC", "points": 25}]}]},
            {"event_date": "2026-03-08", "sessions": [{"session_code": "R", "results": [{"abbreviation": "AAA", "points": 25}]}]},
        ]
    }
    points = championship_points_before(snapshot, "2026-03-08")
    assert points == {"CCC": 25.0}
    weights = standings_weights(names, points)
    assert abs(sum(weights.values()) - 1.0) < 1e-12
    assert max(weights, key=weights.get) == "CCC"


def test_event_config_does_not_inherit_live_gp_parameters() -> None:
    live = {
        "race": "Singapore Grand Prix",
        "safety_car_probability": 0.52,
        "overtaking_difficulty": 0.79,
        "qualifying_noise_scale": 2.0,
        "fixed_grid": ["AAA"],
        "track": {"race_noise": 3.5, "qualifying_noise": 2.2, "tyre_degradation_factor": 0.66},
    }
    cfg = build_event_config(live, {"by_event_name": {}, "by_country": {}}, 2026, 3, "Mystery GP", "Atlantis", "2026-04-01", 500, "race", [], [])
    assert cfg["safety_car_probability"] == 0.22
    assert cfg["overtaking_difficulty"] == 0.5
    assert "qualifying_noise_scale" not in cfg
    assert "fixed_grid" not in cfg


def test_baseline_favourite_is_standings_leader() -> None:
    from pipeline.backtest_simulation import baseline_favourite

    assert baseline_favourite({"VER": 0.2, "NOR": 0.5, "LEC": 0.3}, "NOR") == {"baseline_favourite": "NOR", "baseline_hit": True}
    assert baseline_favourite({"VER": 0.2, "NOR": 0.5}, "VER")["baseline_hit"] is False
    assert baseline_favourite({}, "VER") == {}


def test_grid_weight_search_extends_past_largest_candidate() -> None:
    from pipeline.backtest_simulation import search_grid_weight

    # Loss falls until weight 100, noise scale optimum at 1.5 everywhere.
    def loss(scale: float, weight: float) -> float:
        return abs(weight - 100.0) / 100.0 + abs(scale - 1.5)

    weight, scale, _, report = search_grid_weight(loss, weights=(4.0, 16.0, 48.0))
    assert weight == 108.0  # 48 -> 72 -> 108, then 162 is worse
    assert scale == 1.5
    assert set(report) == {"4.0", "16.0", "48.0", "72.0", "108.0", "162.0"}


def test_start_blend_kept_a_priori_unless_tuned_wins_leave_one_out() -> None:
    from pipeline.backtest_simulation import choose_start_blend

    assert choose_start_blend({"weight": 0.5, "loss": 1.8, "tuned_weight": 0.1, "tuned_leave_one_out_loss": 1.7}) == 0.1
    assert choose_start_blend({"weight": 0.5, "loss": 1.8, "tuned_weight": 0.1, "tuned_leave_one_out_loss": 1.9}) == 0.5


def test_event_config_never_carries_the_live_calibration() -> None:
    from pipeline.backtest_simulation import build_event_config

    live = {"standings_blend_race": 0.5, "standings_blend_qualifying": 0.5, "grid_position_weight": 24.0, "race_noise_scale": 2.0}
    cfg = build_event_config(
        base_config=live, profiles={}, season=2026, round_number=3, event_name="X Grand Prix", country="X",
        event_date="2026-04-01", simulations=500, prediction_target="sprint", inputs_used=[], available_sessions=["SQ"],
        fixed_grid=["AAA", "BBB"],
    )
    for key in ("standings_blend_race", "standings_blend_qualifying", "grid_position_weight", "race_noise_scale"):
        assert key not in cfg
    assert cfg["target_session_code"] == "S" and cfg["fixed_grid"] == ["AAA", "BBB"]


def test_sprint_grid_and_winner_from_the_sprint_session() -> None:
    from pipeline.backtest_simulation import actual_winner_and_podium, fixed_grid_from_event

    event = {"sessions": [
        {"session_code": "S", "results": [
            {"abbreviation": "BBB", "position": 1, "grid_position": 2},
            {"abbreviation": "AAA", "position": 2, "grid_position": 1},
            {"abbreviation": "CCC", "position": 3, "grid_position": 0},
        ]},
        {"session_code": "R", "results": [{"abbreviation": "CCC", "position": 1, "grid_position": 1}]},
    ]}
    assert fixed_grid_from_event(event, "S") == ["AAA", "BBB", "CCC"]
    assert actual_winner_and_podium(event, "S") == ("BBB", {"AAA", "BBB", "CCC"})
    assert actual_winner_and_podium(event)[0] == "CCC"


def test_grid_aware_standings_and_decay_choice() -> None:
    from pipeline.backtest_simulation import choose_grid_decay
    from pipeline.simulate_target_prediction import grid_aware_standings

    standings = {"ANT": 0.5, "VER": 0.25, "RUS": 0.25}
    grid = ["VER", "RUS", "ANT"]
    assert grid_aware_standings(standings, grid, 0.0) == standings
    assert grid_aware_standings(standings, None, 1.0) == standings
    weighted = grid_aware_standings(standings, grid, 1.0)
    assert weighted["VER"] > weighted["ANT"] and abs(sum(weighted.values()) - 1.0) < 1e-9

    # The pole sitter keeps winning while the standings leader starts last:
    # a decay along the grid must be chosen.
    model = {"VER": 0.6, "RUS": 0.3, "ANT": 0.1}
    rows = [(model, standings, grid, "VER")] * 6
    decay, report = choose_grid_decay(rows)
    assert decay > 0 and report["leave_one_out_loss"] < report["no_decay_loss"]
    # No grid information: nothing to gain, decay stays 0.
    assert choose_grid_decay([(model, standings, None, "VER")] * 6)[0] == 0.0
