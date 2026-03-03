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
