#!/usr/bin/env python3
"""
Ingest deterministic Formula 1 hard data from FastF1 into repository JSON.

This script collects completed session results and stores a season snapshot in:
  data/raw/fastf1/season_<YEAR>.json
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


LOGGER = logging.getLogger("ingest_fastf1")

SESSION_ALIASES = {
    "RACE": "R",
    "QUALIFYING": "Q",
    "SPRINT": "S",
    "SPRINT_QUALIFYING": "SQ",
}
VALID_SESSIONS = {"FP1", "FP2", "FP3", "SQ", "S", "Q", "R"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest FastF1 session data into deterministic JSON.")
    parser.add_argument(
        "--season",
        type=int,
        default=datetime.now(timezone.utc).year,
        help="F1 season year (default: current UTC year).",
    )
    parser.add_argument(
        "--sessions",
        default="Q,R",
        help="Comma-separated session codes (default: Q,R). Allowed: FP1,FP2,FP3,SQ,S,Q,R.",
    )
    parser.add_argument(
        "--cutoff-date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="Collect only sessions on/before YYYY-MM-DD (default: today UTC).",
    )
    parser.add_argument(
        "--output-dir",
        default="data/raw/fastf1",
        help="Output directory for season JSON snapshots.",
    )
    parser.add_argument(
        "--cache-dir",
        default="data/raw/fastf1_cache",
        help="FastF1 cache directory (kept inside repository).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def parse_sessions(raw: str) -> list[str]:
    requested: list[str] = []
    seen: set[str] = set()
    for token in raw.split(","):
        value = token.strip().upper()
        if not value:
            continue
        value = SESSION_ALIASES.get(value, value)
        if value not in VALID_SESSIONS:
            raise ValueError(f"Unsupported session code: {value}")
        if value not in seen:
            seen.add(value)
            requested.append(value)
    if not requested:
        raise ValueError("No valid session codes provided.")
    return requested


def parse_iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid --cutoff-date '{raw}' (expected YYYY-MM-DD).") from exc


def to_utc_date(value: Any) -> date | None:
    if value is None:
        return None

    candidate = value
    if hasattr(candidate, "to_pydatetime"):
        candidate = candidate.to_pydatetime()
    elif hasattr(candidate, "item"):
        try:
            candidate = candidate.item()
        except Exception:
            pass

    if isinstance(candidate, datetime):
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=timezone.utc)
        return candidate.astimezone(timezone.utc).date()
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


def to_json_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            return str(value)
        return to_json_scalar(value)

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()

    return str(value)


def normalize_position(value: Any) -> int | None:
    scalar = to_json_scalar(value)
    if scalar is None:
        return None
    try:
        return int(float(str(scalar)))
    except ValueError:
        return None


def extract_results(session: Any) -> list[dict[str, Any]]:
    results = getattr(session, "results", None)
    if results is None or getattr(results, "empty", True):
        return []

    records: list[dict[str, Any]] = []
    for _, row in results.iterrows():
        record = {
            "position": normalize_position(row.get("Position")),
            "grid_position": normalize_position(row.get("GridPosition")),
            "driver_number": to_json_scalar(row.get("DriverNumber")),
            "abbreviation": to_json_scalar(row.get("Abbreviation")),
            "full_name": to_json_scalar(row.get("FullName")),
            "team_name": to_json_scalar(row.get("TeamName")),
            "classified_position": to_json_scalar(row.get("ClassifiedPosition")),
            "status": to_json_scalar(row.get("Status")),
            "points": to_json_scalar(row.get("Points")),
            "time": to_json_scalar(row.get("Time")),
            "q1": to_json_scalar(row.get("Q1")),
            "q2": to_json_scalar(row.get("Q2")),
            "q3": to_json_scalar(row.get("Q3")),
        }
        records.append(record)

    records.sort(
        key=lambda x: (
            x["position"] if x["position"] is not None else 999,
            str(x["abbreviation"] or ""),
            str(x["driver_number"] or ""),
        )
    )
    return records


def load_session(season: int, round_number: int, session_code: str, cutoff: date) -> dict[str, Any] | None:
    try:
        session = fastf1.get_session(season, round_number, session_code)
    except Exception as exc:
        LOGGER.debug("Session lookup failed (%s round %s %s): %s", season, round_number, session_code, exc)
        return None

    session_date = to_utc_date(getattr(session, "date", None))
    if session_date and session_date > cutoff:
        return None

    try:
        session.load(laps=False, telemetry=False, weather=False, messages=False)
    except TypeError:
        # Compatibility with older FastF1 versions.
        session.load(laps=False, telemetry=False, weather=False)
    except Exception as exc:
        LOGGER.warning("Session load failed (%s round %s %s): %s", season, round_number, session_code, exc)
        return None

    results = extract_results(session)
    if not results:
        return None

    return {
        "session_code": session_code,
        "session_name": to_json_scalar(getattr(session, "name", None)),
        "session_date": to_json_scalar(getattr(session, "date", None)),
        "results": results,
    }


def ingest(season: int, sessions: list[str], cutoff: date, output_dir: Path, cache_dir: Path) -> Path:
    if fastf1 is None:
        raise RuntimeError("fastf1 is not installed. Install dependencies from requirements.txt first.")

    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    fastf1.Cache.enable_cache(str(cache_dir))
    schedule = fastf1.get_event_schedule(season, include_testing=False, backend="f1timing")
    schedule = schedule.sort_values(by=["RoundNumber", "EventDate"], kind="stable")

    events_payload: list[dict[str, Any]] = []
    for _, row in schedule.iterrows():
        round_number = normalize_position(row.get("RoundNumber"))
        if round_number is None:
            continue

        event_date = to_utc_date(row.get("EventDate"))
        if event_date and event_date > cutoff:
            continue

        sessions_payload: list[dict[str, Any]] = []
        for session_code in sessions:
            loaded = load_session(season, round_number, session_code, cutoff=cutoff)
            if loaded is not None:
                sessions_payload.append(loaded)

        if not sessions_payload:
            continue

        events_payload.append(
            {
                "round": round_number,
                "event_name": to_json_scalar(row.get("EventName")),
                "official_event_name": to_json_scalar(row.get("OfficialEventName")),
                "country": to_json_scalar(row.get("Country")),
                "location": to_json_scalar(row.get("Location")),
                "event_date": to_json_scalar(row.get("EventDate")),
                "sessions": sessions_payload,
            }
        )

    snapshot = {
        "source": "fastf1",
        "season": season,
        "cutoff_date": cutoff.isoformat(),
        "sessions_requested": sessions,
        "events": events_payload,
    }

    output_path = output_dir / f"season_{season}.json"
    output_path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        sessions = parse_sessions(args.sessions)
        cutoff = parse_iso_date(args.cutoff_date)
        output_path = ingest(
            season=args.season,
            sessions=sessions,
            cutoff=cutoff,
            output_dir=Path(args.output_dir),
            cache_dir=Path(args.cache_dir),
        )
    except Exception as exc:
        LOGGER.error("ingest_fastf1 failed: %s", exc)
        return 1

    LOGGER.info("Wrote FastF1 snapshot: %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
