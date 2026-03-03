#!/usr/bin/env python3
"""
Run deterministic Monte Carlo simulation for dry and wet scenarios.

Inputs:
- models/driver_ratings.json
- models/team_ratings.json
- models/strategy_scores.json
- models/reliability_scores.json
- config/race_config.json

Outputs:
- outputs/prediction_dry.json
- outputs/prediction_wet.json
- outputs/prediction.json (alias to dry scenario for compatibility)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.simulate_race import (  # noqa: E402
    build_entries,
    load_json,
    load_or_default_config,
    run_simulation,
    safe_float,
)


LOGGER = logging.getLogger("simulate_weather_scenarios")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run dry+wet deterministic Monte Carlo simulation.")
    parser.add_argument("--driver-ratings", default="models/driver_ratings.json", help="Driver ratings JSON path.")
    parser.add_argument("--team-ratings", default="models/team_ratings.json", help="Team ratings JSON path.")
    parser.add_argument("--strategy-scores", default="models/strategy_scores.json", help="Strategy scores JSON path.")
    parser.add_argument("--reliability-scores", default="models/reliability_scores.json", help="Reliability scores JSON path.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config JSON path.")
    parser.add_argument("--output-dry", default="outputs/prediction_dry.json", help="Dry scenario output path.")
    parser.add_argument("--output-wet", default="outputs/prediction_wet.json", help="Wet scenario output path.")
    parser.add_argument(
        "--output-default",
        default="outputs/prediction.json",
        help="Default output path kept for compatibility (written from dry scenario).",
    )
    parser.add_argument("--dry-weather-modifier", type=float, default=0.0, help="Weather modifier for dry scenario.")
    parser.add_argument("--wet-weather-modifier", type=float, default=0.3, help="Weather modifier for wet scenario.")
    parser.add_argument("--wet-seed-offset", type=int, default=101, help="Seed offset applied only to wet scenario.")
    parser.add_argument("--allow-missing-models", action="store_true", help="Exit 0 when model files are missing.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def utc_iso_timestamp(now: datetime | None = None) -> str:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_scenario_config(base: dict[str, Any], weather: str, modifier: float, seed_offset: int) -> dict[str, Any]:
    cfg = json.loads(json.dumps(base))
    cfg["weather"] = weather
    cfg["weather_modifier"] = float(modifier)
    base_seed = int(safe_float(cfg.get("seed"), 20260303))
    cfg["seed"] = base_seed + int(seed_offset)
    return cfg


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    required_paths = [
        Path(args.driver_ratings),
        Path(args.team_ratings),
        Path(args.strategy_scores),
        Path(args.reliability_scores),
    ]
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        if args.allow_missing_models:
            LOGGER.warning("Skipping simulation, missing model file(s): %s", ", ".join(missing))
            return 0
        LOGGER.error("simulate_weather_scenarios failed, missing model file(s): %s", ", ".join(missing))
        return 1

    try:
        driver_ratings = load_json(Path(args.driver_ratings))
        team_ratings = load_json(Path(args.team_ratings))
        strategy_scores = load_json(Path(args.strategy_scores))
        reliability_scores = load_json(Path(args.reliability_scores))
        base_config = load_or_default_config(Path(args.race_config))

        entries = build_entries(driver_ratings, team_ratings, strategy_scores, reliability_scores)
        if not entries:
            raise ValueError("No drivers available for simulation. Run update_ratings first.")

        generated_at = utc_iso_timestamp()

        dry_config = make_scenario_config(
            base=base_config,
            weather="dry",
            modifier=args.dry_weather_modifier,
            seed_offset=0,
        )
        dry_config["generated_at"] = generated_at
        wet_config = make_scenario_config(
            base=base_config,
            weather="wet",
            modifier=args.wet_weather_modifier,
            seed_offset=args.wet_seed_offset,
        )
        wet_config["generated_at"] = generated_at

        dry_prediction = run_simulation(entries, dry_config)
        wet_prediction = run_simulation(entries, wet_config)
    except Exception as exc:
        LOGGER.error("simulate_weather_scenarios failed: %s", exc)
        return 1

    dry_path = Path(args.output_dry)
    wet_path = Path(args.output_wet)
    default_path = Path(args.output_default)

    write_json(dry_path, dry_prediction)
    write_json(wet_path, wet_prediction)
    write_json(default_path, dry_prediction)

    LOGGER.info("Wrote dry scenario prediction: %s", dry_path)
    LOGGER.info("Wrote wet scenario prediction: %s", wet_path)
    LOGGER.info("Wrote default prediction alias (dry): %s", default_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
