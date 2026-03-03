#!/usr/bin/env python3
"""
Validate and canonicalize prediction output for publication.

Input:
- outputs/prediction.json (or custom input path)

Output:
- outputs/prediction.json (or custom output path)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("publish_prediction")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and publish prediction JSON.")
    parser.add_argument("--input", default="outputs/prediction.json", help="Input prediction JSON path.")
    parser.add_argument("--output", default="outputs/prediction.json", help="Output prediction JSON path.")
    parser.add_argument("--allow-missing-input", action="store_true", help="Exit 0 when input is missing.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_prediction(payload: dict[str, Any]) -> dict[str, Any]:
    drivers_raw = payload.get("drivers", [])
    if not isinstance(drivers_raw, list):
        raise ValueError("Prediction payload missing 'drivers' list.")

    normalized_drivers: list[dict[str, Any]] = []
    for row in drivers_raw:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue

        win = clamp(safe_float(row.get("win_probability"), 0.0), 0.0, 1.0)
        podium = clamp(safe_float(row.get("podium_probability"), 0.0), 0.0, 1.0)
        podium = max(podium, win)
        expected_finish = max(1.0, safe_float(row.get("expected_finish"), 99.0))

        normalized_drivers.append(
            {
                "name": name,
                "win_probability": round(win, 6),
                "podium_probability": round(podium, 6),
                "expected_finish": round(expected_finish, 6),
            }
        )

    normalized_drivers.sort(key=lambda x: (-x["win_probability"], x["expected_finish"], x["name"].lower()))

    return {
        "race": str(payload.get("race") or "Next GP"),
        "generated_at": str(payload.get("generated_at") or "1970-01-01T00:00:00Z"),
        "drivers": normalized_drivers,
    }


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    input_path = Path(args.input)
    if not input_path.exists():
        if args.allow_missing_input:
            LOGGER.warning("Skipping publish step, prediction input missing: %s", input_path)
            return 0
        LOGGER.error("publish_prediction failed, input missing: %s", input_path)
        return 1

    try:
        raw = load_json(input_path)
        if not isinstance(raw, dict):
            raise ValueError("Prediction input must be a JSON object.")
        normalized = normalize_prediction(raw)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(normalized, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.error("publish_prediction failed: %s", exc)
        return 1

    LOGGER.info("Published prediction to %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
