#!/usr/bin/env python3
"""
Archive processed signals that are older than or equal to the last completed race date.

Signals are moved from:
  knowledge/processed/signals_YYYY-MM-DD.json
to:
  knowledge/processed/archive/signals_YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("archive_old_signals")
SIGNAL_FILE_RE = re.compile(r"^signals_(\d{4}-\d{2}-\d{2})\.json$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive old processed signal files.")
    parser.add_argument("--season", type=int, default=None, help="Season year (defaults to --as-of-date year).")
    parser.add_argument(
        "--as-of-date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="Reference date in YYYY-MM-DD used to detect completed races.",
    )
    parser.add_argument("--raw-dir", default="data/raw/fastf1", help="FastF1 raw snapshot directory.")
    parser.add_argument("--signals-dir", default="knowledge/processed", help="Directory with active signal files.")
    parser.add_argument("--archive-dir", default="knowledge/processed/archive", help="Archive directory.")
    parser.add_argument(
        "--allow-missing-calendar",
        action="store_true",
        help="Exit 0 if calendar snapshot is missing.",
    )
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
        raise ValueError(f"Invalid date '{raw}' (expected YYYY-MM-DD).") from exc


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_calendar_dates(raw_dir: Path, season: int) -> list[date]:
    snapshot = raw_dir / f"season_{season}.json"
    if not snapshot.exists():
        raise FileNotFoundError(f"Missing FastF1 snapshot: {snapshot}")

    payload = load_json(snapshot)
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid FastF1 snapshot payload: {snapshot}")

    calendar = payload.get("calendar")
    if not isinstance(calendar, list):
        raise ValueError(f"FastF1 snapshot missing calendar list: {snapshot}")

    dates: list[date] = []
    for row in calendar:
        if not isinstance(row, dict):
            continue
        raw_date = row.get("event_date")
        if not isinstance(raw_date, str):
            continue
        try:
            dates.append(parse_iso_date(raw_date[:10]))
        except ValueError:
            continue
    return sorted(set(dates))


def latest_completed_race_date(calendar_dates: list[date], as_of: date) -> date | None:
    completed = [d for d in calendar_dates if d < as_of]
    if not completed:
        return None
    return max(completed)


def signal_file_date(path: Path) -> date | None:
    match = SIGNAL_FILE_RE.match(path.name)
    if not match:
        return None
    try:
        return parse_iso_date(match.group(1))
    except ValueError:
        return None


def archive_signals(signals_dir: Path, archive_dir: Path, cutoff_date: date) -> tuple[int, int]:
    if not signals_dir.exists():
        return 0, 0

    candidates = sorted(signals_dir.glob("signals_*.json"))
    scanned = 0
    archived = 0
    archive_dir.mkdir(parents=True, exist_ok=True)

    for src in candidates:
        if src.parent.resolve() == archive_dir.resolve():
            continue
        file_date = signal_file_date(src)
        if file_date is None:
            continue
        scanned += 1
        if file_date > cutoff_date:
            continue

        dst = archive_dir / src.name
        if dst.exists():
            dst.unlink()
        shutil.move(src.as_posix(), dst.as_posix())
        archived += 1

    return scanned, archived


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        as_of = parse_iso_date(args.as_of_date)
        season = args.season if args.season is not None else as_of.year
        calendar_dates = load_calendar_dates(Path(args.raw_dir), season=season)
        cutoff = latest_completed_race_date(calendar_dates, as_of=as_of)
        if cutoff is None:
            LOGGER.info("No completed race before %s; no signal archive action needed.", as_of.isoformat())
            return 0

        scanned, archived = archive_signals(
            signals_dir=Path(args.signals_dir),
            archive_dir=Path(args.archive_dir),
            cutoff_date=cutoff,
        )
    except FileNotFoundError as exc:
        if args.allow_missing_calendar:
            LOGGER.warning("Skipping archive step: %s", exc)
            return 0
        LOGGER.error("archive_old_signals failed: %s", exc)
        return 1
    except Exception as exc:
        LOGGER.error("archive_old_signals failed: %s", exc)
        return 1

    LOGGER.info(
        "Archived %d/%d signal file(s) with file date <= %s.",
        archived,
        scanned,
        cutoff.isoformat(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
