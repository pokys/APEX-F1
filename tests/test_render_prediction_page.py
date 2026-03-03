from __future__ import annotations

import json
from pathlib import Path

from pipeline.render_prediction_page import render_page


def test_render_page_contains_race_and_driver_rows(tmp_path: Path) -> None:
    prediction = {
        "race": "Australian Grand Prix",
        "generated_at": "2026-03-03T00:00:00Z",
        "drivers": [
            {"name": "RUS", "win_probability": 0.31, "podium_probability": 0.75, "expected_finish": 5.0},
            {"name": "VER", "win_probability": 0.28, "podium_probability": 0.72, "expected_finish": 5.4},
            {"name": "PIA", "win_probability": 0.15, "podium_probability": 0.52, "expected_finish": 6.0},
        ],
    }
    race_config = {
        "race_date": "2026-03-08",
        "weather": "dry",
        "simulations": 6000,
        "seed": 202601,
        "safety_car_probability": 0.38,
        "overtaking_difficulty": 0.62,
    }

    rendered = render_page(prediction, race_config)
    assert "Australian Grand Prix" in rendered
    assert "RUS" in rendered
    assert "VER" in rendered
    assert "PIA" in rendered
    assert "Simulations: 6000" in rendered
    assert "Seed: 202601" in rendered
    assert "mobile-list" in rendered
    assert "Top 10" in rendered
    assert "All Drivers" in rendered
    assert "How to read this page" in rendered
    assert "Most likely winner" in rendered


def test_render_page_contains_dry_wet_toggle_when_second_payload_provided() -> None:
    dry_prediction = {
        "race": "Australian Grand Prix",
        "generated_at": "2026-03-03T00:00:00Z",
        "drivers": [
            {"name": "RUS", "win_probability": 0.31, "podium_probability": 0.75, "expected_finish": 5.0},
            {"name": "VER", "win_probability": 0.28, "podium_probability": 0.72, "expected_finish": 5.4},
            {"name": "PIA", "win_probability": 0.15, "podium_probability": 0.52, "expected_finish": 6.0},
        ],
    }
    wet_prediction = {
        "race": "Australian Grand Prix",
        "generated_at": "2026-03-03T00:00:00Z",
        "drivers": [
            {"name": "VER", "win_probability": 0.33, "podium_probability": 0.74, "expected_finish": 4.8},
            {"name": "RUS", "win_probability": 0.26, "podium_probability": 0.69, "expected_finish": 5.7},
            {"name": "NOR", "win_probability": 0.18, "podium_probability": 0.55, "expected_finish": 5.9},
        ],
    }
    race_config = {
        "race_date": "2026-03-08",
        "weather": "dry",
        "simulations": 6000,
        "seed": 202601,
        "safety_car_probability": 0.38,
        "overtaking_difficulty": 0.62,
    }

    rendered = render_page(dry_prediction, race_config, prediction_wet=wet_prediction)
    assert "Dry" in rendered
    assert "Wet" in rendered
    assert "Scenario: Dry" in rendered
    assert "Scenario: Wet" in rendered
    assert "Biggest wet swing" in rendered
