#!/usr/bin/env python3
"""
Apply backtest-derived probability calibration to race configuration.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("apply_backtest_calibration")
BACKTEST_FILE_RE = re.compile(r"^backtest_season_(\d{4})\.json$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply backtest calibration to race_config.")
    parser.add_argument("--season", type=int, default=None, help="Prefer backtest report for this season.")
    parser.add_argument("--backtest-dir", default="outputs/backtest", help="Directory containing backtest reports.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config path to update.")
    parser.add_argument("--min-temp", type=float, default=0.6, help="Minimum allowed win temperature.")
    parser.add_argument("--max-temp", type=float, default=1.8, help="Maximum allowed win temperature.")
    parser.add_argument("--allow-missing-report", action="store_true", help="Exit 0 if no backtest report is present.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def season_from_backtest_filename(path: Path) -> int | None:
    match = BACKTEST_FILE_RE.match(path.name)
    if not match:
        return None
    return int(match.group(1))


def choose_backtest_file(backtest_dir: Path, season: int | None) -> Path | None:
    if not backtest_dir.exists():
        return None
    candidates = sorted(backtest_dir.glob("backtest_season_*.json"))
    if not candidates:
        return None

    if season is not None:
        preferred = backtest_dir / f"backtest_season_{season}.json"
        if preferred.exists():
            return preferred

    # Fall back to highest season number available.
    candidates = sorted(candidates, key=lambda p: season_from_backtest_filename(p) or -1, reverse=True)
    return candidates[0]


def load_or_default_race_config(path: Path) -> dict[str, Any]:
    default = {
        "race": "Next GP",
        "race_date": "1970-01-01",
        "generated_at": "1970-01-01T00:00:00Z",
        "seed": 20260303,
        "simulations": 5000,
        "weather": "dry",
        "weather_modifier": 0.0,
        "safety_car_probability": 0.22,
        "overtaking_difficulty": 0.5,
        "track": {"tyre_degradation_factor": 0.5, "qualifying_noise": 2.6, "race_noise": 3.8},
    }
    if not path.exists():
        return default
    try:
        raw = load_json(path)
    except Exception:
        return default
    if not isinstance(raw, dict):
        return default
    merged = dict(default)
    merged.update({k: v for k, v in raw.items() if k != "track"})
    track = dict(default["track"])
    if isinstance(raw.get("track"), dict):
        track.update(raw["track"])
    merged["track"] = track
    return merged


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        backtest_path = choose_backtest_file(Path(args.backtest_dir), args.season)
        if backtest_path is None:
            if args.allow_missing_report:
                LOGGER.warning("No backtest report found in %s; keeping existing calibration.", args.backtest_dir)
                return 0
            LOGGER.error("No backtest report found in %s.", args.backtest_dir)
            return 1

        report = load_json(backtest_path)
        if not isinstance(report, dict):
            raise ValueError(f"Backtest report is not a JSON object: {backtest_path}")
        summary = report.get("summary")
        if not isinstance(summary, dict):
            raise ValueError(f"Backtest report missing summary: {backtest_path}")
        recommended = summary.get("recommended_win_temperature")
        if not isinstance(recommended, (int, float)):
            raise ValueError(f"Backtest report missing numeric recommended_win_temperature: {backtest_path}")

        win_temp = round(clamp(float(recommended), args.min_temp, args.max_temp), 6)
        race_config_path = Path(args.race_config)
        config = load_or_default_race_config(race_config_path)
        config["win_temperature"] = win_temp
        config["calibration_source"] = str(backtest_path.as_posix())
        config["calibration_season"] = int(report.get("season", args.season or 0))

        race_config_path.parent.mkdir(parents=True, exist_ok=True)
        race_config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.error("apply_backtest_calibration failed: %s", exc)
        return 1

    LOGGER.info("Applied win_temperature=%s from %s", win_temp, backtest_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
