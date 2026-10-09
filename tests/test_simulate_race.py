from __future__ import annotations

from pipeline.simulate_race import smooth_probability_distribution, temperature_scale_distribution


def test_temperature_scale_distribution_preserves_probability_mass() -> None:
    base = {"A": 0.6, "B": 0.3, "C": 0.1}
    scaled = temperature_scale_distribution(base, temperature=1.0)
    assert abs(sum(scaled.values()) - 1.0) < 1e-12
    assert abs(scaled["A"] - 0.6) < 1e-12
    assert abs(scaled["B"] - 0.3) < 1e-12
    assert abs(scaled["C"] - 0.1) < 1e-12


def test_temperature_scale_distribution_flattens_for_higher_temperature() -> None:
    base = {"A": 0.6, "B": 0.3, "C": 0.1}
    hot = temperature_scale_distribution(base, temperature=1.8)
    cold = temperature_scale_distribution(base, temperature=0.6)
    assert hot["A"] < base["A"]
    assert cold["A"] > base["A"]


def test_smooth_probability_distribution_removes_hard_zeroes() -> None:
    base = {"A": 1.0, "B": 0.0, "C": 0.0}
    smoothed = smooth_probability_distribution(base, smoothing=0.001)
    assert abs(sum(smoothed.values()) - 1.0) < 1e-12
    assert smoothed["A"] < 1.0
    assert smoothed["B"] > 0.0
    assert smoothed["C"] > 0.0


def test_smooth_probability_distribution_can_be_disabled() -> None:
    base = {"A": 0.6, "B": 0.4, "C": 0.0}
    smoothed = smooth_probability_distribution(base, smoothing=0.0)
    assert smoothed == base


import random

from pipeline.simulate_race import simulate_qualifying, simulate_single_race


def _entry(name, rating=60.0, strategy=50.0, dnf=0.0, wet=50.0):
    return {
        "name": name,
        "team": name,
        "driver_rating": rating,
        "team_rating": rating,
        "qualifying_rating": rating,
        "race_rating": rating,
        "qualifying_team_rating": rating,
        "race_team_rating": rating,
        "strategy_score": strategy,
        "reliability_score": 90.0,
        "dnf_probability": dnf,
        "wet_rating": wet,
    }


def _race_counts(entries, kind, runs=3000, wet=False):
    rng = random.Random(7)
    wins = {e["name"]: 0 for e in entries}
    dnfs = 0
    for _ in range(runs):
        result = simulate_single_race(entries, [e["name"] for e in entries], rng, 0.2, 0.5, 0.0, 0.5, 3.0, race_kind=kind, wet=wet)
        for name, pos in result.items():
            if pos == 1:
                wins[name] += 1
            if pos > len(entries):
                dnfs += 1
    return wins, dnfs


def test_sprint_has_a_third_of_race_dnf_exposure() -> None:
    entries = [_entry("AAA", dnf=0.3), _entry("BBB", dnf=0.3)]
    _, race_dnfs = _race_counts(entries, "race")
    _, sprint_dnfs = _race_counts(entries, "sprint")
    assert 0.25 < sprint_dnfs / race_dnfs < 0.42


def test_sprint_ignores_pit_strategy() -> None:
    strong = [_entry("AAA", strategy=90.0), _entry("BBB", strategy=10.0)]
    weak = [_entry("AAA", strategy=10.0), _entry("BBB", strategy=90.0)]
    race_strong, _ = _race_counts(strong, "race")
    race_weak, _ = _race_counts(weak, "race")
    sprint_strong, _ = _race_counts(strong, "sprint")
    sprint_weak, _ = _race_counts(weak, "sprint")
    # Strategy decides grands prix, but makes no difference in a sprint.
    assert race_strong["AAA"] - race_weak["AAA"] > 1500
    assert sprint_strong["AAA"] == sprint_weak["AAA"]


def test_wet_rating_only_matters_in_the_wet() -> None:
    entries = [_entry("RAIN", wet=95.0), _entry("SUN", wet=50.0)]
    rng = random.Random(3)
    dry_poles = sum(simulate_qualifying(entries, rng, 2.0)[0] == "RAIN" for _ in range(3000))
    wet_poles = sum(simulate_qualifying(entries, rng, 2.0, wet=True)[0] == "RAIN" for _ in range(3000))
    assert 0.45 < dry_poles / 3000 < 0.55
    assert wet_poles / 3000 > 0.85


def test_dry_scenario_keeps_track_weather_modifier() -> None:
    from pipeline.simulate_weather_scenarios import make_scenario_config

    base = {"weather_modifier": 0.14, "seed": 10}
    assert make_scenario_config(base, "dry", None, 0)["weather_modifier"] == 0.14
    assert round(make_scenario_config(base, "wet", None, 101)["weather_modifier"], 6) == 0.44


def test_qualifying_probabilities_are_scaled_consistently() -> None:
    from pipeline.simulate_target_prediction import run_qualifying_prediction

    entries = [_entry(f"D{i:02d}", rating=80.0 - 3.0 * i) for i in range(20)]
    config = {"seed": 5, "simulations": 5000, "qualifying_temperature": 1.8, "track": {"qualifying_noise": 2.0}}
    rows = run_qualifying_prediction(entries, config, {}, {}, {})["drivers"]
    for row in rows:
        assert row["pole_probability"] <= row["front_row_probability"] + 1e-9
        assert row["front_row_probability"] <= row["top10_probability"] + 1e-9
    assert abs(sum(row["pole_probability"] for row in rows) - 1.0) < 1e-6
    assert abs(sum(row["front_row_probability"] for row in rows) - 2.0) < 1e-6


def test_standings_blend_mixes_whole_outcomes_consistently() -> None:
    from pipeline.simulate_target_prediction import run_qualifying_prediction

    entries = [_entry("FAST", rating=90.0), _entry("SLOW", rating=10.0)]
    standings = {"FAST": 0.2, "SLOW": 0.8}
    config = {"seed": 1, "simulations": 6000, "standings_blend_qualifying": 0.5, "track": {"qualifying_noise": 1.0}}
    rows = {row["name"]: row for row in run_qualifying_prediction(entries, config, {}, {}, {}, standings)["drivers"]}
    # Model gives FAST pole ~always; standings model gives SLOW 80 %.
    assert abs(rows["SLOW"]["pole_probability"] - 0.4) < 0.03
    # Half model (always 2nd), half standings (1st with 0.8): 0.5*2 + 0.5*1.2.
    assert abs(rows["SLOW"]["expected_position"] - 1.6) < 0.03


def test_grid_weight_controls_how_much_the_starting_grid_matters() -> None:
    from pipeline.simulate_race import DEFAULT_GRID_POSITION_WEIGHT, grid_weight_from_config

    # The fastest driver starts last; a heavy grid weight keeps the pole
    # sitter ahead at a track where overtaking is hard.
    entries = [_entry("FAST", rating=75.0)] + [_entry(f"D{i}", rating=60.0) for i in range(9)]
    grid = [f"D{i}" for i in range(9)] + ["FAST"]

    def pole_wins(weight: float) -> int:
        rng = random.Random(7)
        return sum(
            simulate_single_race(entries, grid, rng, 0.0, 0.8, 0.0, 0.5, 3.0, race_kind="sprint", grid_weight=weight)["D0"] == 1
            for _ in range(400)
        )

    assert pole_wins(40.0) > pole_wins(DEFAULT_GRID_POSITION_WEIGHT) + 100
    assert grid_weight_from_config({}) == DEFAULT_GRID_POSITION_WEIGHT
    assert grid_weight_from_config({"grid_position_weight": 24}) == 24.0
    assert grid_weight_from_config({"grid_position_weight": "x"}) == DEFAULT_GRID_POSITION_WEIGHT
    assert grid_weight_from_config({"grid_position_weight": 1e9}) == 2000.0
