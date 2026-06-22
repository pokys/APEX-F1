#!/usr/bin/env python3
"""
Validate backtest quality metrics against configured gates.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("validate_backtest_quality")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate backtest quality metrics.")
    parser.add_argument("--report", default="outputs/backtest/backtest_season_2025.json", help="Backtest report JSON path.")
    parser.add_argument("--gate-config", default="config/backtest_quality_gates.json", help="Quality gate JSON path.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def require_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric, got boolean.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric, got {value!r}.") from exc


def validate_backtest_quality(report: dict[str, Any], gates: dict[str, Any]) -> None:
    summary = require_mapping(report.get("summary"), "report.summary")

    minimum_counts = require_mapping(gates.get("minimum_counts", {}), "gates.minimum_counts")
    for metric, minimum in minimum_counts.items():
        actual = require_number(report.get(metric), f"report.{metric}")
        limit = require_number(minimum, f"gates.minimum_counts.{metric}")
        if actual < limit:
            raise ValueError(f"{metric} below quality gate: expected >= {limit}, got {actual}")

    summary_max = require_mapping(gates.get("summary_max", {}), "gates.summary_max")
    for metric, maximum in summary_max.items():
        actual = require_number(summary.get(metric), f"report.summary.{metric}")
        limit = require_number(maximum, f"gates.summary_max.{metric}")
        if actual > limit:
            raise ValueError(f"{metric} above quality gate: expected <= {limit}, got {actual}")

    summary_min = require_mapping(gates.get("summary_min", {}), "gates.summary_min")
    for metric, minimum in summary_min.items():
        actual = require_number(summary.get(metric), f"report.summary.{metric}")
        limit = require_number(minimum, f"gates.summary_min.{metric}")
        if actual < limit:
            raise ValueError(f"{metric} below quality gate: expected >= {limit}, got {actual}")


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        report = require_mapping(load_json(Path(args.report)), "report")
        gates = require_mapping(load_json(Path(args.gate_config)), "gates")
        validate_backtest_quality(report, gates)
    except Exception as exc:
        LOGGER.error("validate_backtest_quality failed: %s", exc)
        return 1

    LOGGER.info("Backtest quality gates passed for %s", args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
