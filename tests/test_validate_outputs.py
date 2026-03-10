from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.validate_outputs import validate_prediction


def test_validate_prediction_accepts_consistent_distribution(tmp_path: Path) -> None:
    path = tmp_path / "prediction.json"
    path.write_text(
        json.dumps(
            {
                "race": "Test GP",
                "generated_at": "2026-01-01T00:00:00Z",
                "drivers": [
                    {"name": "A", "win_probability": 0.4, "podium_probability": 0.9, "expected_finish": 2.0},
                    {"name": "B", "win_probability": 0.35, "podium_probability": 0.85, "expected_finish": 2.5},
                    {"name": "C", "win_probability": 0.25, "podium_probability": 0.7, "expected_finish": 3.0},
                    {"name": "D", "win_probability": 0.0, "podium_probability": 0.55, "expected_finish": 4.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    validate_prediction(path)


def test_validate_prediction_rejects_invalid_win_sum(tmp_path: Path) -> None:
    path = tmp_path / "prediction_invalid.json"
    path.write_text(
        json.dumps(
            {
                "drivers": [
                    {"name": "A", "win_probability": 0.7, "podium_probability": 0.9, "expected_finish": 2.0},
                    {"name": "B", "win_probability": 0.7, "podium_probability": 0.9, "expected_finish": 2.5},
                    {"name": "C", "win_probability": 0.0, "podium_probability": 0.7, "expected_finish": 3.0},
                    {"name": "D", "win_probability": 0.0, "podium_probability": 0.5, "expected_finish": 4.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="sum\\(win_probability\\)"):
        validate_prediction(path)


def test_validate_prediction_accepts_qualifying_distribution(tmp_path: Path) -> None:
    path = tmp_path / "quali_prediction.json"
    path.write_text(
        json.dumps(
            {
                "prediction_target": "qualifying",
                "target_output_type": "qualifying",
                "drivers": [
                    {"name": "A", "pole_probability": 0.4, "front_row_probability": 0.7, "top10_probability": 1.0, "expected_position": 2.1},
                    {"name": "B", "pole_probability": 0.35, "front_row_probability": 0.65, "top10_probability": 1.0, "expected_position": 2.4},
                    {"name": "C", "pole_probability": 0.25, "front_row_probability": 0.4, "top10_probability": 1.0, "expected_position": 3.6},
                    {"name": "D", "pole_probability": 0.0, "front_row_probability": 0.25, "top10_probability": 1.0, "expected_position": 4.4},
                ],
            }
        ),
        encoding="utf-8",
    )
    validate_prediction(path)
