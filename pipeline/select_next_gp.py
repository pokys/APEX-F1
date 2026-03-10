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
    parser.add_argument("--raw-dir", default="data/raw/fastf1", help="FastF1 raw snapshot directory.")
    parser.add_argument("--profiles", default="config/track_profiles.json", help="Track profile config path.")
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


def utc_iso_timestamp(now: datetime | None = None) -> str:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def load_track_profiles(path: Path) -> dict[str, Any]:
    default_profiles = {
        "by_event_name": {
            "australian grand prix": {
                "overtaking_difficulty": 0.62,
                "safety_car_probability": 0.38,
                "weather": "dry",
                "weather_modifier": 0.05,
                "track": {
                    "tyre_degradation_factor": 0.56,
                    "qualifying_noise": 2.4,
                    "race_noise": 3.6,
                },
            }
        },
        "by_country": {},
    }
    if not path.exists():
        return default_profiles
    try:
        raw = load_json(path)
    except Exception:
        return default_profiles
    if not isinstance(raw, dict):
        return default_profiles

    merged = dict(default_profiles)
    for key in ("by_event_name", "by_country"):
        if isinstance(raw.get(key), dict):
            merged[key] = raw[key]
    return merged


def apply_track_profile(config: dict[str, Any], event: dict[str, Any], profiles: dict[str, Any]) -> str | None:
    event_name_key = str(event.get("event_name") or "").strip().lower()
    country_key = str(event.get("country") or "").strip().lower()
    profile = None
    profile_name = None

    by_event = profiles.get("by_event_name", {})
    by_country = profiles.get("by_country", {})
    if isinstance(by_event, dict) and event_name_key in by_event and isinstance(by_event[event_name_key], dict):
        profile = by_event[event_name_key]
        profile_name = f"event:{event_name_key}"
    elif isinstance(by_country, dict) and country_key in by_country and isinstance(by_country[country_key], dict):
        profile = by_country[country_key]
        profile_name = f"country:{country_key}"

    if profile is None:
        return None

    for key in ("overtaking_difficulty", "safety_car_probability", "weather", "weather_modifier", "simulations"):
        if key in profile:
            config[key] = profile[key]

    if isinstance(profile.get("track"), dict):
        track = config.get("track")
        if not isinstance(track, dict):
            track = {}
        track.update(profile["track"])
        config["track"] = track

    return profile_name


def normalize_calendar_event(event: dict[str, Any], season: int) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    event_date = to_utc_date(event.get("event_date"))
    if event_date is None:
        return None
    try:
        round_number = int(float(event.get("round")))
    except Exception:
        round_number = 0
    return {
        "season": season,
        "round": round_number,
        "event_name": str(event.get("event_name") or "Next GP"),
        "official_event_name": str(event.get("official_event_name") or event.get("event_name") or "Next GP"),
        "event_format": str(event.get("event_format") or ""),
        "country": str(event.get("country") or ""),
        "location": str(event.get("location") or ""),
        "event_date": event_date.isoformat(),
    }


def load_snapshot_calendar(raw_dir: Path, season: int) -> list[dict[str, Any]]:
    path = raw_dir / f"season_{season}.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    calendar = payload.get("calendar")
    if not isinstance(calendar, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in calendar:
        if not isinstance(item, dict):
            continue
        normalized_item = normalize_calendar_event(item, season=season)
        if normalized_item is not None:
            normalized.append(normalized_item)
    normalized.sort(key=lambda x: (x["event_date"], x["round"], x["event_name"]))
    return normalized


def next_event_from_calendar(calendar: list[dict[str, Any]], as_of: date, raw_dir: Path) -> dict[str, Any] | None:
    for event in calendar:
        event_date = to_utc_date(event.get("event_date"))
        if event_date is None:
            continue
        
        # If the race is today, check if we already have race results in the raw data
        if event_date == as_of:
            if has_race_results(raw_dir, event["season"], event["event_name"]):
                LOGGER.info("Race results detected for today's GP (%s). Moving to next.", event["event_name"])
                continue

        if event_date < as_of:
            continue
        return event
    return None


def has_race_results(raw_dir: Path, season: int, event_name: str) -> bool:
    path = raw_dir / f"season_{season}.json"
    if not path.exists():
        return False
    try:
        payload = load_json(path)
    except Exception:
        return False
    events = payload.get("events", [])
    event_name_lower = event_name.strip().lower()
    for event in events:
        if str(event.get("event_name") or "").strip().lower() == event_name_lower:
            for session in event.get("sessions", []):
                if str(session.get("session_code") or "").upper() == "R":
                    results = session.get("results", [])
                    # If results list is not empty, data is arriving
                    return len(results) > 0
    return False


def next_event_for_season(season: int, as_of: date, raw_dir: Path) -> dict[str, Any] | None:
    if fastf1 is None:
        return None
    backends = ["f1timing", "ergast"]
    for backend in backends:
        try:
            schedule = fastf1.get_event_schedule(season, include_testing=False, backend=backend)
        except Exception as exc:
            LOGGER.warning("Schedule fetch failed for season %s backend %s: %s", season, backend, exc)
            continue

        schedule = schedule.sort_values(by=["EventDate", "RoundNumber"], kind="stable")
        for _, row in schedule.iterrows():
            event_date = to_utc_date(row.get("EventDate"))
            if event_date is None:
                continue
            
            event_name = str(row.get("EventName") or "Next GP")
            
            # Skip if date is in the past
            if event_date < as_of:
                continue
            
            # If today is race day, check if we already have results
            if event_date == as_of:
                if has_race_results(raw_dir, season, event_name):
                    LOGGER.info("Race results detected for today's GP (%s) via live schedule. Moving to next.", event_name)
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


def select_next_event(requested_season: int | None, as_of: date, raw_dir: Path) -> dict[str, Any]:
    base_season = requested_season if requested_season is not None else as_of.year
    candidate_seasons = [base_season, base_season + 1]

    for season in candidate_seasons:
        local_calendar = load_snapshot_calendar(raw_dir, season=season)
        local_event = next_event_from_calendar(local_calendar, as_of=as_of, raw_dir=raw_dir)
        if local_event is not None:
            LOGGER.info("Selected next GP from local snapshot calendar for season %s.", season)
            return local_event

        event = next_event_for_season(season, as_of, raw_dir=raw_dir)
        if event is not None:
            return event

    raise RuntimeError(
        f"No upcoming GP found for seasons {candidate_seasons} as of {as_of.isoformat()}."
    )


def extract_fixed_grid(raw_dir: Path, season: int, event_name: str) -> list[str] | None:
    path = raw_dir / f"season_{season}.json"
    if not path.exists():
        return None
    try:
        payload = load_json(path)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    events = payload.get("events")
    if not isinstance(events, list):
        return None

    target_event = None
    event_name_lower = event_name.strip().lower()
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("event_name") or "").strip().lower() == event_name_lower:
            target_event = event
            break

    if not target_event:
        return None

    sessions = target_event.get("sessions")
    if not isinstance(sessions, list):
        return None

    qualifying_results = None
    for session in sessions:
        if not isinstance(session, dict):
            continue
        if str(session.get("session_code") or "").upper() == "Q":
            results = session.get("results")
            if isinstance(results, list) and len(results) > 0:
                qualifying_results = results
                break

    if not qualifying_results:
        return None

    # Sort results by position and extract driver abbreviations
    scored: list[tuple[int, str]] = []
    for res in qualifying_results:
        if not isinstance(res, dict):
            continue
        try:
            pos = int(float(res.get("position")))
            name = str(res.get("abbreviation") or res.get("full_name") or "").strip()
            if name:
                scored.append((pos, name))
        except (TypeError, ValueError):
            continue

    if not scored:
        return None

    scored.sort()
    return [name for _, name in scored]


def get_available_sessions(raw_dir: Path, season: int, event_name: str) -> list[str]:
    path = raw_dir / f"season_{season}.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception:
        return []
    
    available = []
    events = payload.get("events", [])
    event_name_lower = event_name.strip().lower()
    for event in events:
        if str(event.get("event_name") or "").strip().lower() == event_name_lower:
            for session in event.get("sessions", []):
                results = session.get("results", [])
                if results and len(results) > 0:
                    code = str(session.get("session_code") or "").upper()
                    if code:
                        available.append(code)
    return available


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        as_of = parse_iso_date(args.as_of_date)
        path = Path(args.race_config)
        config = load_existing_config(path)
        event = select_next_event(args.season, as_of, raw_dir=Path(args.raw_dir))
        profiles = load_track_profiles(Path(args.profiles))

        config["season"] = event["season"]
        config["next_round"] = event["round"]
        config["race"] = event["event_name"]
        config["location"] = event.get("location", "")
        config["race_date"] = event["event_date"]
        config["event_format"] = event.get("event_format", "")
        config["generated_at"] = utc_iso_timestamp()
        
        # Track available sessions for debug info
        config["available_sessions"] = get_available_sessions(Path(args.raw_dir), event["season"], event["event_name"])

        # Stable race-specific seed to keep deterministic simulation per selected GP.
        config["seed"] = int(f"{event['season']}{event['round']:02d}")
        if config.get("simulations", 0) < 5000:
            config["simulations"] = 5000
        profile_name = apply_track_profile(config, event, profiles)
        if profile_name:
            config["track_profile"] = profile_name

        config["grid_source"] = "simulation"
        config.pop("fixed_grid", None)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    except Exception as exc:
        # Non-fatal fallback: keep the last known config to avoid breaking the full pipeline on transient API issues.
        LOGGER.warning("Using existing race config due to next-GP selection failure: %s", exc)
        path = Path(args.race_config)
        if path.exists():
            return 0
        LOGGER.error("select_next_gp failed and no existing race config is available.")
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
