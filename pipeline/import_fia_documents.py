#!/usr/bin/env python3
"""
Best-effort import of grid penalties from the stewards' decision documents
published by the FIA (PDF) for every Grand Prix.

Race control (import_penalties.py) misses penalties that are only published
as documents, most importantly power-unit element changes ("Drop of 25 grid
positions for the next Race ...") and parc fermé changes ("Required to
start the Race from the pit lane"). This script reads the "Infringement" and
"Decision" documents of the upcoming GP, plus the race decisions of the
previous GP (penalties carried over to the next event), and writes
grid_penalty event signals to knowledge/processed/penalties_<season>_fia.json.

The official entry list of the upcoming GP is compared with the current
line-up (latest competitive session); a driver who replaces a regular one
becomes a driver_substitution signal, so substitutes are in the prediction
before they ever set a lap.

Plain text parsing with pypdf, no AI involved. Already processed documents
are remembered in the output file, so each run downloads only new PDFs.

To stay polite to fia.com the site is only checked during the race weekend
(from a day before the first session until the race start), at most once
every CHECK_INTERVAL_HOURS. Outside that window nothing is requested.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import logging
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.prediction_targeting import _parse_iso_datetime, load_json  # noqa: E402
from pipeline.update_ratings import load_current_entry_list  # noqa: E402

LOGGER = logging.getLogger("import_fia_documents")

FIA_BASE = "https://www.fia.com"
CHAMPIONSHIP_PATH = "/documents/championships/fia-formula-one-world-championship-14"
SOURCE_NAME = "fia_decision_documents"
SOURCE_CONFIDENCE = 0.98
CHECK_INTERVAL_HOURS = 2
WINDOW_BEFORE_FIRST_SESSION = timedelta(hours=24)
USER_AGENT = "APEX-F1 prediction pipeline (+https://github.com/pokys/apex-f1)"

OPTION_RE = re.compile(r'<option[^>]*value="([^"]+)"[^>]*>([^<]+)')
PDF_LINK_RE = re.compile(r'href="(/system/files/decision-document/[^"]+\.pdf)"')
# Stewards' decisions about a car; summonses and lists are skipped.
ENTRY_LIST_DOC_RE = re.compile(r"_-_entry_list", re.IGNORECASE)
ENTRY_ROW_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Z]{3})\s+(.+?)\s+([A-Z]{3})\s+(.+?)\s*$")
DOCUMENT_NO_RE = re.compile(r"Document\s+(\d+)")
# A parsed entry list shorter than this is treated as a parsing failure.
MIN_ENTRY_LIST_SIZE = 18
DECISION_DOC_RE = re.compile(r"_-_(?:infringement|decision|offence)_-_car_\d+", re.IGNORECASE)

DRIVER_RE = re.compile(r"No\s*/\s*Driver\s+(\d+)\s*-\s*([^\n]+)")
SESSION_RE = re.compile(r"\nSession\s+([^\n]+)")
DATE_RE = re.compile(r"\nDate\s+(\d{1,2}\s+\w+\s+\d{4})")
TIME_RE = re.compile(r"\nTime\s+(\d{1,2}:\d{2})")
DECISION_RE = re.compile(r"\nDecision\s+(.*?)\n(?:Reason|Competitors are reminded)", re.DOTALL)

PLACES_RE = re.compile(r"(?:DROP\s+OF\s+(\d+)\s+GRID\s+(?:POSITION|PLACE)S?|(\d+)\s*-?\s*(?:GRID\s+)?PLACES?\s+GRID\s+PENALTY)")
PIT_LANE_RE = re.compile(r"START\s+(?:THE\s+(?:RACE|SPRINT)\s+)?FROM\s+(?:THE\s+)?PIT\s*LANE")
BACK_OF_GRID_RE = re.compile(r"BACK\s+OF\s+THE\s+GRID")

SESSION_CODES = {
    "FREE PRACTICE 1": "FP1",
    "FREE PRACTICE 2": "FP2",
    "FREE PRACTICE 3": "FP3",
    "SPRINT QUALIFYING": "SQ",
    "SPRINT SHOOTOUT": "SQ",
    "SPRINT": "S",
    "QUALIFYING": "Q",
    "RACE": "R",
}
# Sessions of a sprint weekend before the sprint starts.
BEFORE_SPRINT = {"FP1", "SQ"}

Fetcher = Callable[[str], bytes]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import grid penalties from FIA stewards' decision documents.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config of the upcoming GP.")
    parser.add_argument("--calendar-cache-dir", default="data/raw/calendars", help="Calendar cache directory.")
    parser.add_argument("--raw-dir", default="data/raw/fastf1", help="FastF1 snapshots (car number -> driver code).")
    parser.add_argument("--signals-dir", default="knowledge/processed", help="Signals directory to write into.")
    parser.add_argument("--now", default=None, help="UTC ISO time used as 'now' (default: current time).")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def should_check(schedule: dict[str, Any], last_checked: Any, now: datetime) -> tuple[bool, str]:
    """Whether to contact fia.com now, and why (for the log)."""
    starts = [s for s in (_parse_iso_datetime(v) for v in (schedule or {}).values()) if s is not None]
    if not starts:
        return False, "no session schedule"
    race_start = _parse_iso_datetime((schedule or {}).get("R")) or max(starts)
    if now < min(starts) - WINDOW_BEFORE_FIRST_SESSION:
        return False, "race weekend has not started yet"
    if now > race_start:
        return False, "race already started"
    last = _parse_iso_datetime(last_checked)
    if last is not None and now - last < timedelta(hours=CHECK_INTERVAL_HOURS):
        return False, f"checked less than {CHECK_INTERVAL_HOURS} h ago"
    return True, "race weekend"


def http_fetch(url: str, attempts: int = 3) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https host
                return response.read()
        except Exception as exc:
            last_exc = exc
            LOGGER.warning("FIA request attempt %s/%s failed for %s: %s", attempt, attempts, url, exc)
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"FIA website unavailable: {last_exc}")


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader  # imported lazily: only this importer needs it

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def page_options(page: str) -> list[tuple[str, str]]:
    return [(html.unescape(value), " ".join(html.unescape(label).split())) for value, label in OPTION_RE.findall(page)]


def season_url(championship_page: str, season: int) -> str:
    for value, label in page_options(championship_page):
        if label.upper() == f"SEASON {season}":
            return FIA_BASE + value
    raise LookupError(f"No FIA documents page for season {season}")


def event_url(season_page: str, event_name: str) -> str | None:
    key = event_name.strip().lower()
    for value, label in page_options(season_page):
        if label.lower() == key and "/event/" in value:
            return FIA_BASE + value
    return None


def decision_document_links(event_page: str) -> list[str]:
    links = {FIA_BASE + path for path in PDF_LINK_RE.findall(event_page)}
    return sorted(link for link in links if DECISION_DOC_RE.search(link.rsplit("/", 1)[-1]))


def entry_list_links(event_page: str) -> list[str]:
    links = {FIA_BASE + path for path in PDF_LINK_RE.findall(event_page)}
    return sorted(link for link in links if ENTRY_LIST_DOC_RE.search(link.rsplit("/", 1)[-1]))


def parse_entry_list(text: str) -> list[dict[str, Any]]:
    """Rows "81 PIA Oscar Piastri AUS McLaren Mastercard F1 Team McLaren
    Mercedes" -> number, code, name and the rest (entrant + constructor)."""
    rows = []
    for line in text.splitlines():
        match = ENTRY_ROW_RE.match(line)
        if match:
            rows.append(
                {
                    "number": int(match.group(1)),
                    "code": match.group(2),
                    "name": match.group(3),
                    "team_text": " ".join(match.group(5).split()),
                }
            )
    return rows


def team_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find_substitutions(entry: list[dict[str, Any]], roster: dict[str, str]) -> list[dict[str, str]]:
    """Regular drivers missing from the entry list and who replaces them.
    The team of a newcomer comes from an entry-list teammate who is already
    in the line-up; a newcomer without such a teammate is matched by team
    name against the line-up."""
    if len(entry) < MIN_ENTRY_LIST_SIZE or not roster:
        return []
    listed = {row["code"] for row in entry}
    missing = {code: team for code, team in roster.items() if code not in listed}
    out = []
    for row in entry:
        if row["code"] in roster:
            continue
        mates = [r["code"] for r in entry if r["team_text"] == row["team_text"] and r["code"] in roster]
        team = roster[mates[0]] if mates else next(
            (t for t in set(roster.values()) if team_key(t) and team_key(t) in team_key(row["team_text"])), None
        )
        replaced = sorted(code for code, t in missing.items() if t == team)
        if team and replaced:
            missing.pop(replaced[0])
            out.append({"driver_out": replaced[0], "driver_in": row["code"], "team": team, "name": row["name"]})
    return out


def substitution_signal(sub: dict[str, str], season: int, event_name: str, url: str, timestamp: str | None) -> dict[str, Any]:
    return {
        "type": "driver_substitution",
        "season": season,
        "event": event_name,
        "driver_out": sub["driver_out"],
        "driver_in": sub["driver_in"],
        "team": sub["team"],
        "source_name": SOURCE_NAME,
        "source_url": url,
        "source_confidence": SOURCE_CONFIDENCE,
        "timestamp": timestamp or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "evidence": f"{sub['name']} ({sub['driver_in']}) is on the entry list instead of {sub['driver_out']}",
    }


def document_timestamp(text: str) -> str | None:
    date, clock = DATE_RE.search(text), TIME_RE.search(text)
    if not date:
        return None
    try:
        return datetime.strptime(f"{date.group(1)} {clock.group(1) if clock else '00:00'}", "%d %B %Y %H:%M").isoformat()
    except ValueError:
        return None


def parse_decision(text: str) -> dict[str, Any] | None:
    """Car, session and grid sanction of one stewards' decision document,
    or None when it holds no grid sanction (fines, reprimands, time
    penalties, no further action)."""
    driver = DRIVER_RE.search(text)
    decision = DECISION_RE.search(text)
    if not driver or not decision:
        return None
    decision_text = " ".join(decision.group(1).split())
    upper = decision_text.upper()
    parsed: dict[str, Any] = {
        "driver_number": int(driver.group(1)),
        "driver_name": " ".join(driver.group(2).split()),
        "decision": decision_text,
    }
    places = PLACES_RE.search(upper)
    if places:
        parsed["places"] = int(places.group(1) or places.group(2))
    elif PIT_LANE_RE.search(upper):
        parsed["pit_lane"] = True
    elif BACK_OF_GRID_RE.search(upper):
        parsed["back_of_grid"] = True
    else:
        return None
    session = SESSION_RE.search(text)
    session_label = " ".join(session.group(1).split()).upper() if session else ""
    parsed["session"] = SESSION_CODES.get(session_label, session_label or None)
    if "SPRINT/RACE" in upper:
        parsed["target"] = "sprint_or_race"
    elif "SPRINT" in upper and "RACE" not in upper:
        parsed["target"] = "sprint"
    else:
        parsed["target"] = "race"
    stamp = document_timestamp(text)
    if stamp:
        parsed["timestamp"] = stamp
    return parsed


def applies_to(parsed: dict[str, Any], sprint_weekend: bool, previous_event: bool) -> str | None:
    """Which start of the upcoming GP the sanction affects. None when it
    belonged to an earlier start (previous GP decisions not taken in the
    race and not carried over)."""
    session = parsed.get("session")
    if previous_event:
        # Only decisions taken in (or after) the previous race carry over.
        if session not in {"R", None}:
            return None
        return "sprint" if (sprint_weekend and parsed["target"] != "race") else "race"
    if session == "R":
        return None
    if parsed["target"] == "sprint":
        return "sprint" if sprint_weekend else None
    if parsed["target"] == "sprint_or_race" and sprint_weekend and session in BEFORE_SPRINT:
        return "sprint"
    return "race"


def name_key(name: Any) -> str:
    text = unicodedata.normalize("NFKD", " ".join(str(name or "").split()))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def driver_codes(raw_dir: Path, season: int) -> tuple[dict[int, str], dict[str, str]]:
    """Car number -> code and full name -> code from the FastF1 snapshots
    (latest session wins, so mid-season number changes are followed)."""
    by_number: dict[int, str] = {}
    by_name: dict[str, str] = {}
    for year in (season - 1, season):
        path = raw_dir / f"season_{year}.json"
        if not path.exists():
            continue
        snapshot = load_json(path)
        for event in snapshot.get("events") or []:
            for session in event.get("sessions") or []:
                for row in session.get("results") or []:
                    code = str(row.get("abbreviation") or "").strip().upper()
                    if len(code) != 3:
                        continue
                    try:
                        by_number[int(row.get("driver_number"))] = code
                    except (TypeError, ValueError):
                        pass
                    if row.get("full_name"):
                        by_name[name_key(row["full_name"])] = code
    return by_number, by_name


def resolve_driver(parsed: dict[str, Any], by_number: dict[int, str], by_name: dict[str, str]) -> str | None:
    name = name_key(parsed.get("driver_name"))
    if name in by_name:
        return by_name[name]
    return by_number.get(parsed["driver_number"])


def penalty_signal(parsed: dict[str, Any], driver: str, season: int, event_name: str, target: str, url: str) -> dict[str, Any]:
    signal: dict[str, Any] = {
        "type": "grid_penalty",
        "season": season,
        "event": event_name,
        "driver": driver,
        "applies_to": target,
        "source_name": SOURCE_NAME,
        "source_url": url,
        "source_confidence": SOURCE_CONFIDENCE,
        "timestamp": parsed.get("timestamp") or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "evidence": parsed["decision"],
    }
    for key in ("places", "back_of_grid", "pit_lane"):
        if key in parsed:
            signal[key] = parsed[key]
    return signal


def neighbouring_events(calendar: list[dict[str, Any]], event_name: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    events = sorted((e for e in calendar if isinstance(e, dict)), key=lambda e: str(e.get("event_date") or ""))
    index = next((i for i, e in enumerate(events) if str(e.get("event_name") or "").lower() == event_name.lower()), None)
    if index is None:
        return None, None
    return events[index], (events[index - 1] if index > 0 else None)


def collect(
    fetch: Fetcher,
    season: int,
    event_name: str,
    sprint_weekend: bool,
    previous_event_name: str | None,
    processed: set[str],
    by_number: dict[int, str],
    by_name: dict[str, str],
    roster: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], set[str], list[dict[str, Any]] | None]:
    """New penalty signals, the document URLs read in this run, and the
    substitution signals of a newly read entry list (None: no new list)."""
    season_page = fetch(season_url(fetch(FIA_BASE + CHAMPIONSHIP_PATH).decode("utf-8", "replace"), season)).decode("utf-8", "replace")
    signals: list[dict[str, Any]] = []
    done: set[str] = set()
    substitutions: list[dict[str, Any]] | None = None
    scans = [(event_name, False)] + ([(previous_event_name, True)] if previous_event_name else [])
    for name, previous in scans:
        url = event_url(season_page, name)
        if url is None:
            LOGGER.info("No FIA documents listed for %s yet.", name)
            continue
        page = fetch(url).decode("utf-8", "replace")
        if not previous and roster:
            substitutions = read_entry_lists(fetch, page, processed, done, roster, season, event_name)
        for link in decision_document_links(page):
            if link in processed:
                continue
            try:
                parsed = parse_decision(pdf_text(fetch(link)))
            except Exception as exc:
                LOGGER.warning("Could not read %s: %s", link, exc)
                continue
            done.add(link)
            if parsed is None:
                continue
            target = applies_to(parsed, sprint_weekend, previous)
            if target is None:
                continue
            driver = resolve_driver(parsed, by_number, by_name)
            if driver is None:
                LOGGER.warning("Unknown car %s (%s) in %s", parsed["driver_number"], parsed["driver_name"], link)
                done.discard(link)  # retry once the snapshot knows the driver
                continue
            signals.append(penalty_signal(parsed, driver, season, event_name, target, link))
    return signals, done, substitutions


def read_entry_lists(
    fetch: Fetcher,
    event_page: str,
    processed: set[str],
    done: set[str],
    roster: dict[str, str],
    season: int,
    event_name: str,
) -> list[dict[str, Any]] | None:
    """Substitutions from the newest entry list (highest document number)
    when an unread one was published, else None."""
    newest: tuple[int, str, str] | None = None
    for link in entry_list_links(event_page):
        if link in processed:
            continue
        try:
            text = pdf_text(fetch(link))
        except Exception as exc:
            LOGGER.warning("Could not read %s: %s", link, exc)
            continue
        done.add(link)
        number = DOCUMENT_NO_RE.search(text)
        key = int(number.group(1)) if number else 0
        if newest is None or key >= newest[0]:
            newest = (key, link, text)
    if newest is None:
        return None
    _, link, text = newest
    entry = parse_entry_list(text)
    if len(entry) < MIN_ENTRY_LIST_SIZE:
        LOGGER.warning("Entry list %s parsed into only %s rows; ignored.", link, len(entry))
        return None
    stamp = document_timestamp(text)
    return [substitution_signal(sub, season, event_name, link, stamp) for sub in find_substitutions(entry, roster)]


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")
    try:
        config = load_json(Path(args.race_config))
        season = int(config.get("season"))
        event_name = str(config.get("race") or "")
        calendar_path = Path(args.calendar_cache_dir) / f"season_{season}.json"
        calendar = load_json(calendar_path) if calendar_path.exists() else []
        current, previous = neighbouring_events(calendar, event_name)
        sprint_weekend = str(config.get("event_format") or (current or {}).get("event_format") or "").lower().startswith("sprint")
        out_path = Path(args.signals_dir) / f"penalties_{season}_fia.json"
        existing = load_json(out_path) if out_path.exists() else {}
        existing_signals = [s for s in existing.get("signals") or [] if isinstance(s, dict)]
        processed = {str(u) for u in existing.get("processed_documents") or []}
        now = _parse_iso_datetime(args.now) if args.now else datetime.now(timezone.utc)
        schedule = config.get("sessions_schedule") if isinstance(config.get("sessions_schedule"), dict) else {}
        check, reason = should_check(schedule, existing.get("last_checked"), now)
        if not check:
            LOGGER.info("FIA documents not checked: %s.", reason)
            return 0
        by_number, by_name = driver_codes(Path(args.raw_dir), season)
        previous_name = str(previous.get("event_name") or "") or None if previous else None
        roster, _ = load_current_entry_list(Path(args.raw_dir), season)
        found, done, substitutions = collect(
            http_fetch, season, event_name, sprint_weekend, previous_name, processed, by_number, by_name, roster
        )
        if not found and not done:
            LOGGER.info("No new FIA decision documents for %s.", event_name)
        if substitutions is not None:
            # The newest entry list replaces what an older one said.
            existing_signals = [
                s
                for s in existing_signals
                if not (s.get("type") == "driver_substitution" and str(s.get("event") or "").lower() == event_name.lower())
            ]
            found = found + substitutions
            for sub in substitutions:
                LOGGER.info("Substitution: %s replaces %s (%s)", sub["driver_in"], sub["driver_out"], sub["team"])
        def key(s: dict[str, Any]) -> tuple:
            return (s.get("source_url"), s.get("event"), s.get("driver") or s.get("driver_in"))

        known = {key(s) for s in existing_signals}
        merged = existing_signals + [s for s in found if key(s) not in known]
        merged.sort(key=lambda s: (str(s.get("event")), str(s.get("timestamp")), str(s.get("driver") or s.get("driver_in"))))
        payload = {
            "signals": merged,
            "processed_documents": sorted(processed | done),
            "last_checked": now.replace(microsecond=0).isoformat(),
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
        LOGGER.info("FIA documents: %s new decisions read, %s new penalties, written to %s", len(done), len(found), out_path)
    except Exception as exc:
        # Best effort: never block the prediction pipeline.
        LOGGER.warning("import_fia_documents skipped: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
