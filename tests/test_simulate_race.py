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
