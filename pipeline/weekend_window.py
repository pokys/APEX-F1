#!/usr/bin/env python3
"""
Is the upcoming GP's race weekend running right now?

Used by the Race Weekend Keeper workflow, which triggers the full pipeline
every hour during a race weekend. GitHub delays scheduled (cron) runs of
this repository by hours, while workflow_dispatch runs start at once.

The window opens a day before the first session (entry list, PU
penalties) and closes a few hours after the race start, so the race
result is ingested and scored and the pipeline moves on to the next GP.
Prints "in" or "out".
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.prediction_targeting import _parse_iso_datetime, load_json  # noqa: E402

OPEN_BEFORE_FIRST_SESSION = timedelta(hours=24)
CLOSE_AFTER_RACE_START = timedelta(hours=6)


def in_weekend_window(schedule: dict[str, Any] | None, now: datetime) -> bool:
    starts = [s for s in (_parse_iso_datetime(v) for v in (schedule or {}).values()) if s is not None]
    if not starts:
        return False
    race_start = _parse_iso_datetime((schedule or {}).get("R")) or max(starts)
    return min(starts) - OPEN_BEFORE_FIRST_SESSION <= now <= race_start + CLOSE_AFTER_RACE_START


def main() -> int:
    parser = argparse.ArgumentParser(description="Print 'in' during the upcoming GP's race weekend, else 'out'.")
    parser.add_argument("--race-config", default="config/race_config.json")
    parser.add_argument("--now", default=None, help="UTC ISO time used as 'now' (default: current time).")
    args = parser.parse_args()
    now = _parse_iso_datetime(args.now) if args.now else datetime.now(timezone.utc)
    try:
        config = load_json(Path(args.race_config))
    except Exception:
        config = {}
    schedule = config.get("sessions_schedule") if isinstance(config, dict) else None
    print("in" if in_weekend_window(schedule if isinstance(schedule, dict) else None, now) else "out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
