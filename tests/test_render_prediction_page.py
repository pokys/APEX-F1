from __future__ import annotations

from pipeline.render_prediction_page import render_page


def test_render_page_shows_current_target_and_inputs() -> None:
    prediction = {
        "race": "Australian Grand Prix",
        "generated_at": "2026-03-03T12:34:56Z",
        "prediction_target": "qualifying",
        "prediction_target_label": "Qualifying",
        "target_session_code": "Q",
        "target_output_type": "qualifying",
        "weekend_format": "standard",
        "inputs_used": [
            {"source": "history_driver", "source_key": "history_driver", "weight": 0.5},
            {"source": "fp3", "source_key": "FP3", "weight": 0.3},
            {"source": "signals", "source_key": "signals", "weight": 0.2},
        ],
        "simulation": {"simulations": 6000, "available_sessions": ["FP1", "FP2", "FP3"]},
        "drivers": [
            {"name": "RUS", "team": "Mercedes", "pole_probability": 0.31, "front_row_probability": 0.6, "top10_probability": 0.99, "expected_position": 2.1},
            {"name": "VER", "team": "Red Bull", "pole_probability": 0.28, "front_row_probability": 0.58, "top10_probability": 0.98, "expected_position": 2.4},
            {"name": "PIA", "team": "McLaren", "pole_probability": 0.15, "front_row_probability": 0.33, "top10_probability": 0.96, "expected_position": 4.0},
        ],
    }
    race_config = {
        "signal_count": 3,
        "grid_source": "simulation",
    }

    rendered = render_page(prediction, race_config)
    assert "Now Predicting" in rendered
    assert "Qualifying" in rendered
    assert "Sessions Online" in rendered
    assert "FP1, FP2, FP3" in rendered
    assert "Input Weights" in rendered
    assert "Weekend Timeline" in rendered
    assert "Why This Is Active Now" in rendered
    assert "Technical Details" in rendered
    assert "history_driver" in rendered
    assert "Pole" in rendered
    assert "Expected Position" in rendered


def test_render_page_shows_dry_wet_toggle_for_race_predictions() -> None:
    dry_prediction = {
        "race": "Chinese Grand Prix",
        "generated_at": "2026-03-10T12:00:00Z",
        "prediction_target": "race",
        "prediction_target_label": "Race",
        "target_session_code": "R",
        "target_output_type": "race",
        "weekend_format": "standard",
        "simulation": {"simulations": 6000, "available_sessions": ["Q"], "grid_source": "qualifying"},
        "drivers": [
            {"name": "RUS", "team": "Mercedes", "win_probability": 0.31, "podium_probability": 0.75, "expected_finish": 3.2},
            {"name": "VER", "team": "Red Bull", "win_probability": 0.28, "podium_probability": 0.72, "expected_finish": 3.5},
            {"name": "PIA", "team": "McLaren", "win_probability": 0.15, "podium_probability": 0.52, "expected_finish": 4.3},
        ],
    }
    wet_prediction = {
        "race": "Chinese Grand Prix",
        "generated_at": "2026-03-10T12:00:00Z",
        "prediction_target": "race",
        "prediction_target_label": "Race",
        "target_session_code": "R",
        "target_output_type": "race",
        "weekend_format": "standard",
        "simulation": {"simulations": 6000, "available_sessions": ["Q"], "grid_source": "qualifying"},
        "drivers": [
            {"name": "VER", "team": "Red Bull", "win_probability": 0.33, "podium_probability": 0.74, "expected_finish": 3.1},
            {"name": "RUS", "team": "Mercedes", "win_probability": 0.26, "podium_probability": 0.69, "expected_finish": 3.7},
            {"name": "NOR", "team": "McLaren", "win_probability": 0.18, "podium_probability": 0.55, "expected_finish": 4.2},
        ],
    }

    rendered = render_page(dry_prediction, {}, prediction_wet=wet_prediction)
    assert "Dry" in rendered
    assert "Wet" in rendered
    assert "Race" in rendered
    assert "Win" in rendered
    assert "Podium" in rendered
