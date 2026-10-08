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
