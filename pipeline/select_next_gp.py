#!/usr/bin/env python3
"""
Select the next GP from FastF1 schedule and update race configuration.

Output:
- config/race_config.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fastf1  # type: ignore
except ModuleNotFoundError:
    fastf1 = None


LOGGER = logging.getLogger("select_next_gp")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select next GP and update race_config.json.")
    parser.add_argument("--season", type=int, default=None, help="Target season (default: current UTC year).")
    parser.add_argument(
        "--as-of-date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="Reference date in YYYY-MM-DD used for selecting the next GP.",
    )
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config path.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def parse_iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid --as-of-date '{raw}' (expected YYYY-MM-DD).") from exc


def is_na_like(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        if value != value:
            return True
    except Exception:
        pass
    return str(value).strip().lower() in {"", "none", "nan", "nat"}


def to_utc_date(value: Any) -> date | None:
    if is_na_like(value):
        return None

    candidate = value
    if hasattr(candidate, "to_pydatetime"):
        try:
            candidate = candidate.to_pydatetime()
        except Exception:
            return None
        if is_na_like(candidate):
            return None
    elif hasattr(candidate, "item"):
        try:
            candidate = candidate.item()
        except Exception:
            pass
        if is_na_like(candidate):
            return None

    if isinstance(candidate, datetime):
        try:
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=timezone.utc)
            return candidate.astimezone(timezone.utc).date()
        except Exception:
            return None
    if isinstance(candidate, date):
        return candidate
    if isinstance(candidate, str):
        text = candidate.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_existing_config(path: Path) -> dict[str, Any]:
    default: dict[str, Any] = {
        "race": "Next GP",
        "race_date": "1970-01-01",
        "generated_at": "1970-01-01T00:00:00Z",
        "seed": 20260303,
        "simulations": 5000,
        "weather": "dry",
        "weather_modifier": 0.0,
        "safety_car_probability": 0.22,
        "overtaking_difficulty": 0.5,
        "track": {
            "tyre_degradation_factor": 0.5,
            "qualifying_noise": 2.6,
            "race_noise": 3.8,
        },
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


def next_event_for_season(season: int, as_of: date) -> dict[str, Any] | None:
    schedule = fastf1.get_event_schedule(season, include_testing=False, backend="f1timing")
    schedule = schedule.sort_values(by=["EventDate", "RoundNumber"], kind="stable")

    for _, row in schedule.iterrows():
        event_date = to_utc_date(row.get("EventDate"))
        if event_date is None:
            continue
        if event_date < as_of:
            continue
        round_number = row.get("RoundNumber")
        try:
            round_number = int(float(round_number))
        except Exception:
            round_number = 0

        return {
            "season": season,
            "round": round_number,
            "event_name": str(row.get("EventName") or "Next GP"),
            "official_event_name": str(row.get("OfficialEventName") or row.get("EventName") or "Next GP"),
            "country": str(row.get("Country") or ""),
            "location": str(row.get("Location") or ""),
            "event_date": event_date.isoformat(),
        }
    return None


def select_next_event(requested_season: int | None, as_of: date) -> dict[str, Any]:
    if fastf1 is None:
        raise RuntimeError("fastf1 is not installed. Install dependencies from requirements.txt first.")

    base_season = requested_season if requested_season is not None else as_of.year
    candidate_seasons = [base_season, base_season + 1]

    for season in candidate_seasons:
        event = next_event_for_season(season, as_of)
        if event is not None:
            return event

    raise RuntimeError(
        f"No upcoming GP found for seasons {candidate_seasons} as of {as_of.isoformat()}."
    )


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        as_of = parse_iso_date(args.as_of_date)
        event = select_next_event(args.season, as_of)
        path = Path(args.race_config)
        config = load_existing_config(path)

        config["season"] = event["season"]
        config["next_round"] = event["round"]
        config["race"] = event["event_name"]
        config["race_date"] = event["event_date"]
        config["generated_at"] = f"{as_of.isoformat()}T00:00:00Z"
        # Stable race-specific seed to keep deterministic simulation per selected GP.
        config["seed"] = int(f"{event['season']}{event['round']:02d}")
        if config.get("simulations", 0) < 5000:
            config["simulations"] = 5000

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    except Exception as exc:
        LOGGER.error("select_next_gp failed: %s", exc)
        return 1

    LOGGER.info(
        "Selected next GP: %s (%s, season %s round %s)",
        event["event_name"],
        event["event_date"],
        event["season"],
        event["round"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
