#!/usr/bin/env python3
"""
Best-effort import of grid penalties from OpenF1 race control messages.

Race control publishes stewards' decisions as text, e.g.
  "FIA STEWARDS: 3 PLACE GRID PENALTY FOR CAR 23 (ALB) - IMPEDING"
  "CAR 31 (OCO) WILL START FROM THE PIT LANE"
This script scans the sessions of the upcoming GP that have already run,
plus the previous GP's race/sprint (for penalties carried over to the next
event), and writes grid_penalty event signals to
knowledge/processed/penalties_<season>_auto.json. The output passes the
same validation as hand-written signals (validate_signals.py).

Not every penalty reaches race control in this form: power-unit element
changes are published in FIA documents (PDF) only. Those must still be
added by hand as signals (see knowledge/processed/README.md).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.openf1_client import BASE_URL, OpenF1Client  # noqa: E402
from pipeline.prediction_targeting import _parse_iso_datetime, load_json  # noqa: E402

LOGGER = logging.getLogger("import_penalties")

SOURCE_NAME = "openf1_race_control"
SOURCE_CONFIDENCE = 0.9

CAR_RE = re.compile(r"CAR\s+(\d+)\s*\(([A-Z]{3})\)")
PLACES_RE = re.compile(r"(\d+)\s*-?\s*(?:GRID\s+)?(?:PLACE|POSITION)S?\s+(?:GRID\s+)?(?:PENALTY|DROP)")
PLACES_ALT_RE = re.compile(r"GRID\s+(?:PENALTY|DROP)\s+OF\s+(\d+)\s+(?:PLACE|POSITION)S?")
PIT_LANE_RE = re.compile(r"STARTS?\s+(?:THE\s+RACE\s+)?FROM\s+(?:THE\s+)?PIT\s*LANE")
BACK_OF_GRID_RE = re.compile(r"BACK\s+OF\s+THE\s+GRID")
NEXT_EVENT_RE = re.compile(r"NEXT\s+(?:RACE|EVENT|GRAND\s+PRIX|ROUND|COMPETITION)")
NON_DECISION_RE = re.compile(r"UNDER\s+INVESTIGATION|WILL\s+BE\s+INVESTIGATED|NO\s+FURTHER\s+ACTION|\bNOTED\b|REVIEWED|AFTER\s+THE\s+RACE")

# Session code -> which grid a penalty from that session affects.
SPRINT_GRID_SESSIONS = {"SQ"}
SCANNED_CURRENT = ("FP1", "FP2", "FP3", "SQ", "S", "Q")
SCANNED_PREVIOUS = ("S", "R")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import grid penalties from OpenF1 race control messages.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config of the upcoming GP.")
    parser.add_argument("--calendar-cache-dir", default="data/raw/calendars", help="Calendar cache directory.")
    parser.add_argument("--signals-dir", default="knowledge/processed", help="Signals directory to write into.")
    parser.add_argument("--now", default=None, help="UTC ISO time used as 'now' (default: current time).")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def parse_penalty_message(message: str) -> dict[str, Any] | None:
    """Grid sanction announced in a race control message, or None."""
    text = " ".join(str(message or "").upper().split())
    if not text or NON_DECISION_RE.search(text):
        return None
    car = CAR_RE.search(text)
    if not car:
        return None
    parsed: dict[str, Any] = {"driver_number": int(car.group(1)), "driver": car.group(2)}
    places = PLACES_RE.search(text) or PLACES_ALT_RE.search(text)
    if places:
        parsed["places"] = int(places.group(1))
    elif BACK_OF_GRID_RE.search(text):
        parsed["back_of_grid"] = True
    elif PIT_LANE_RE.search(text):
        parsed["pit_lane"] = True
    else:
        return None
    parsed["next_event"] = bool(NEXT_EVENT_RE.search(text))
    return parsed


def penalty_signal(
    parsed: dict[str, Any],
    season: int,
    event_name: str,
    applies_to: str,
    session_key: int,
    message: dict[str, Any],
) -> dict[str, Any]:
    signal: dict[str, Any] = {
        "type": "grid_penalty",
        "season": season,
        "event": event_name,
        "driver": parsed["driver"],
        "applies_to": applies_to,
        "source_name": SOURCE_NAME,
        "source_url": f"{BASE_URL}/race_control?session_key={session_key}",
        "source_confidence": SOURCE_CONFIDENCE,
        "timestamp": str(message.get("date") or ""),
        "evidence": " ".join(str(message.get("message") or "").split()),
    }
    for key in ("places", "back_of_grid", "pit_lane"):
        if key in parsed:
            signal[key] = parsed[key]
    return signal


def signals_from_session(
    client: OpenF1Client,
    season: int,
    session_code: str,
    scheduled_start: Any,
    current_event: str,
    is_previous_event: bool,
) -> list[dict[str, Any]]:
    session_key = client.find_session_key(season, session_code, scheduled_start)
    if session_key is None:
        return []
    out = []
    for message in client.race_control(session_key):
        parsed = parse_penalty_message(str(message.get("message") or ""))
        if parsed is None:
            continue
        # Penalties from the previous GP only matter if carried over.
        if is_previous_event and not parsed["next_event"]:
            continue
        if not is_previous_event and parsed["next_event"]:
            continue
        applies_to = "sprint" if (not is_previous_event and session_code in SPRINT_GRID_SESSIONS) else "race"
        signal = penalty_signal(parsed, season, current_event, applies_to, session_key, message)
        if not signal["timestamp"]:
            signal["timestamp"] = str(scheduled_start)
        out.append(signal)
    return out


def signal_key(signal: dict[str, Any]) -> tuple:
    return (
        signal.get("season"),
        str(signal.get("event") or "").lower(),
        signal.get("driver"),
        signal.get("applies_to"),
        signal.get("evidence"),
    )


def merge_signals(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {signal_key(s): s for s in existing}
    for signal in new:
        merged.setdefault(signal_key(signal), signal)
    return sorted(merged.values(), key=lambda s: (str(s.get("event")), str(s.get("timestamp")), str(s.get("driver"))))


def collect_penalties(
    client: OpenF1Client,
    season: int,
    calendar: list[dict[str, Any]],
    event_name: str,
    now: datetime,
) -> list[dict[str, Any]]:
    events = sorted((e for e in calendar if isinstance(e, dict)), key=lambda e: str(e.get("event_date") or ""))
    index = next((i for i, e in enumerate(events) if str(e.get("event_name") or "").lower() == event_name.lower()), None)
    if index is None:
        return []
    scans: list[tuple[dict[str, Any], tuple[str, ...], bool]] = [(events[index], SCANNED_CURRENT, False)]
    if index > 0:
        scans.append((events[index - 1], SCANNED_PREVIOUS, True))
    signals: list[dict[str, Any]] = []
    for event, codes, previous in scans:
        schedule = event.get("sessions_schedule") if isinstance(event.get("sessions_schedule"), dict) else {}
        for code in codes:
            start = _parse_iso_datetime(schedule.get(code))
            if start is None or start > now:
                continue
            try:
                signals.extend(signals_from_session(client, season, code, schedule.get(code), event_name, previous))
            except Exception as exc:
                LOGGER.warning("Race control import failed for %s %s: %s", event.get("event_name"), code, exc)
    return signals


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")
    now = _parse_iso_datetime(args.now) if args.now else datetime.now(timezone.utc)
    try:
        config = load_json(Path(args.race_config))
        season = int(config.get("season"))
        event_name = str(config.get("race") or "")
        calendar_path = Path(args.calendar_cache_dir) / f"season_{season}.json"
        calendar = load_json(calendar_path) if calendar_path.exists() else []
        found = collect_penalties(OpenF1Client(), season, calendar, event_name, now)
        out_path = Path(args.signals_dir) / f"penalties_{season}_auto.json"
        existing: list[dict[str, Any]] = []
        if out_path.exists():
            raw = load_json(out_path)
            existing = [s for s in (raw.get("signals") if isinstance(raw, dict) else raw) or [] if isinstance(s, dict)]
        merged = merge_signals(existing, found)
        if merged and merged != existing:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps({"signals": merged}, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
            LOGGER.info("Wrote %s penalty signals to %s", len(merged), out_path)
        else:
            LOGGER.info("No new penalties for %s.", event_name)
    except Exception as exc:
        # Best effort: never block the prediction pipeline.
        LOGGER.warning("import_penalties skipped: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
