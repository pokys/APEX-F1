#!/usr/bin/env python3
"""
Validate pipeline artifacts for deterministic consistency.

Checks:
- expected files exist for chosen season
- model files share the same season
- prediction probability invariants hold
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("validate_outputs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate generated pipeline artifacts.")
    parser.add_argument("--season", type=int, required=True, help="Season to validate (e.g. 2025).")
    parser.add_argument("--raw-dir", default="data/raw/fastf1", help="FastF1 raw directory.")
    parser.add_argument("--processed-dir", default="data/processed", help="Processed data directory.")
    parser.add_argument("--models-dir", default="models", help="Models directory.")
    parser.add_argument("--prediction", default="outputs/prediction.json", help="Prediction JSON path.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def assert_exists(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")


def assert_close(value: float, expected: float, tolerance: float, label: str) -> None:
    if abs(value - expected) > tolerance:
        raise ValueError(f"{label} expected {expected} ± {tolerance}, got {value}")


def validate_prediction(path: Path) -> None:
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise ValueError("Prediction payload must be a JSON object.")

    rows = payload.get("drivers")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Prediction payload must include non-empty 'drivers' list.")

    win_sum = 0.0
    podium_sum = 0.0
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Prediction driver row #{idx} is not an object.")
        name = str(row.get("name") or "").strip()
        if not name:
            raise ValueError(f"Prediction driver row #{idx} has empty name.")

        win = float(row.get("win_probability"))
        podium = float(row.get("podium_probability"))
        exp_finish = float(row.get("expected_finish"))

        if not (0.0 <= win <= 1.0):
            raise ValueError(f"win_probability out of bounds for {name}: {win}")
        if not (0.0 <= podium <= 1.0):
            raise ValueError(f"podium_probability out of bounds for {name}: {podium}")
        if podium < win:
            raise ValueError(f"podium_probability must be >= win_probability for {name}")
        if exp_finish < 1.0:
            raise ValueError(f"expected_finish must be >= 1 for {name}")

        win_sum += win
        podium_sum += podium

    # Rounding in persisted output is 6 dp, so allow tiny tolerance.
    assert_close(win_sum, 1.0, 1e-3, "sum(win_probability)")
    assert_close(podium_sum, 3.0, 1e-3, "sum(podium_probability)")


def validate_model_file(path: Path, season: int, key: str) -> None:
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} payload must be object.")
    file_season = payload.get("season")
    if int(file_season) != season:
        raise ValueError(f"{path} season mismatch: expected {season}, got {file_season}")
    if key not in payload or not isinstance(payload[key], list):
        raise ValueError(f"{path} missing expected list key '{key}'.")


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    season = args.season
    raw_path = Path(args.raw_dir) / f"season_{season}.json"
    features_path = Path(args.processed_dir) / f"features_season_{season}.json"
    driver_path = Path(args.models_dir) / "driver_ratings.json"
    team_path = Path(args.models_dir) / "team_ratings.json"
    strategy_path = Path(args.models_dir) / "strategy_scores.json"
    reliability_path = Path(args.models_dir) / "reliability_scores.json"
    prediction_path = Path(args.prediction)

    try:
        assert_exists(raw_path)
        assert_exists(features_path)
        assert_exists(driver_path)
        assert_exists(team_path)
        assert_exists(strategy_path)
        assert_exists(reliability_path)
        assert_exists(prediction_path)

        validate_model_file(driver_path, season, "drivers")
        validate_model_file(team_path, season, "teams")
        validate_model_file(strategy_path, season, "teams")
        validate_model_file(reliability_path, season, "teams")
        validate_prediction(prediction_path)
    except Exception as exc:
        LOGGER.error("Validation failed: %s", exc)
        return 1

    LOGGER.info("Validation passed for season %s", season)
    return 0


if __name__ == "__main__":
    sys.exit(main())
