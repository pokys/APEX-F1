from __future__ import annotations

from pipeline.simulate_race import temperature_scale_distribution


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
