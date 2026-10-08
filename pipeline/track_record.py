#!/usr/bin/env python3
"""
Archive predictions before each session starts and score them afterwards.

- archive: copies the current prediction to
  outputs/archive/<season>/<round>_<session>.json while the target session
  has not started yet. Once it starts the file is frozen, so the archive
  holds the last prediction made before the session.
- evaluate: scores every archived prediction whose session now has results
  in the FastF1 snapshot and writes outputs/track_record.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.prediction_targeting import _parse_iso_datetime, load_json  # noqa: E402

LOGGER = logging.getLogger("track_record")

QUALIFYING_CODES = {"Q", "SQ"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive pre-session predictions and score them afterwards.")
    parser.add_argument("--prediction", default="outputs/prediction.json", help="Published prediction JSON.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config JSON.")
    parser.add_argument("--archive-dir", default="outputs/archive", help="Archive directory.")
    parser.add_argument("--raw-dir", default="data/raw/fastf1", help="FastF1 raw snapshot directory.")
    parser.add_argument("--output", default="outputs/track_record.json", help="Track record JSON output.")
    parser.add_argument("--now", default=None, help="UTC ISO time used as 'now' (default: current time).")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def archive_path(archive_dir: Path, season: int, round_number: int, session_code: str) -> Path:
    return archive_dir / str(season) / f"{round_number:02d}_{session_code}.json"


def archive_prediction(prediction: dict[str, Any], race_config: dict[str, Any], archive_dir: Path, now: datetime) -> Path | None:
    """Store the prediction if its target session has not started yet.
    Returns the written path, or None when nothing was archived."""
    try:
        season = int(race_config.get("season"))
        round_number = int(race_config.get("next_round"))
    except (TypeError, ValueError):
        return None
    code = str(prediction.get("target_session_code") or race_config.get("target_session_code") or "").upper()
    schedule = race_config.get("sessions_schedule") if isinstance(race_config.get("sessions_schedule"), dict) else {}
    start = _parse_iso_datetime(schedule.get(code)) if code else None
    if not code or start is None:
        LOGGER.info("No scheduled start for %s; not archiving.", code or "target session")
        return None
    if now >= start:
        LOGGER.info("Session %s already started at %s; archive stays frozen.", code, start.isoformat())
        return None
    path = archive_path(archive_dir, season, round_number, code)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "season": season,
        "round": round_number,
        "race": prediction.get("race") or race_config.get("race"),
        "session_code": code,
        "session_start": start.isoformat(),
        "archived_at": now.replace(microsecond=0).isoformat(),
        "prediction_target": prediction.get("prediction_target"),
        "target_output_type": prediction.get("target_output_type"),
        "drivers": prediction.get("drivers") or [],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


def session_classification(snapshot: dict[str, Any], round_number: int, code: str) -> list[str]:
    for event in snapshot.get("events") or []:
        if not isinstance(event, dict):
            continue
        try:
            if int(event.get("round")) != round_number:
                continue
        except (TypeError, ValueError):
            continue
        for session in event.get("sessions") or []:
            if not isinstance(session, dict) or str(session.get("session_code") or "").upper() != code:
                continue
            ranked = []
            for row in session.get("results") or []:
                try:
                    position = int(float(row.get("position")))
                except (TypeError, ValueError, AttributeError):
                    continue
                abbr = str(row.get("abbreviation") or "").strip().upper()
                if abbr:
                    ranked.append((position, abbr))
            return [abbr for _, abbr in sorted(ranked)]
    return []


def score_entry(entry: dict[str, Any], order: list[str]) -> dict[str, Any] | None:
    if not order:
        return None
    qualifying = str(entry.get("session_code") or "").upper() in QUALIFYING_CODES
    headline = "pole_probability" if qualifying else "win_probability"
    group_key, group_size = ("top10_probability", 10) if qualifying else ("podium_probability", 3)
    drivers = [row for row in entry.get("drivers") or [] if isinstance(row, dict) and row.get("name")]
    if not drivers:
        return None
    probs = {str(row["name"]).upper(): float(row.get(headline) or 0.0) for row in drivers}
    actual = order[0]
    predicted = max(probs, key=lambda name: (probs[name], name))
    predicted_group = [
        str(row["name"]).upper()
        for row in sorted(drivers, key=lambda r: (-float(r.get(group_key) or 0.0), str(r["name"])))[:group_size]
    ]
    return {
        "season": entry.get("season"),
        "round": entry.get("round"),
        "race": entry.get("race"),
        "session_code": entry.get("session_code"),
        "archived_at": entry.get("archived_at"),
        "actual_winner": actual,
        "predicted_winner": predicted,
        "hit": predicted == actual,
        "winner_probability": round(probs.get(actual, 0.0), 6),
        # Floor at 1/1000 so one unforeseen result does not dominate the mean.
        "log_loss": round(-math.log(max(probs.get(actual, 0.0), 1e-3)), 6),
        "group": "top10" if qualifying else "podium",
        "group_overlap": len(set(predicted_group) & set(order[:group_size])),
    }


def evaluate_archive(archive_dir: Path, raw_dir: Path) -> dict[str, Any]:
    snapshots: dict[int, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for path in sorted(archive_dir.glob("*/*.json")):
        try:
            entry = load_json(path)
            season = int(entry.get("season"))
            round_number = int(entry.get("round"))
        except Exception as exc:
            LOGGER.warning("Skipping unreadable archive entry %s: %s", path, exc)
            continue
        if season not in snapshots:
            snapshot_path = raw_dir / f"season_{season}.json"
            snapshots[season] = load_json(snapshot_path) if snapshot_path.exists() else {}
        order = session_classification(snapshots[season], round_number, str(entry.get("session_code") or "").upper())
        scored = score_entry(entry, order)
        if scored is not None:
            entries.append(scored)

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"count": 0}
        return {
            "count": len(rows),
            "hit_rate": round(sum(1 for r in rows if r["hit"]) / len(rows), 6),
            "mean_log_loss": round(sum(r["log_loss"] for r in rows) / len(rows), 6),
            "mean_winner_probability": round(sum(r["winner_probability"] for r in rows) / len(rows), 6),
            "mean_group_overlap": round(sum(r["group_overlap"] for r in rows) / len(rows), 6),
        }

    by_session: dict[str, list[dict[str, Any]]] = {}
    for row in entries:
        by_session.setdefault(str(row["session_code"]), []).append(row)
    return {
        "summary": summarize(entries),
        "by_session": {code: summarize(rows) for code, rows in sorted(by_session.items())},
        "entries": sorted(entries, key=lambda r: (r["season"], r["round"], str(r["session_code"]))),
    }


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")
    now = _parse_iso_datetime(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        LOGGER.error("Invalid --now value: %s", args.now)
        return 1
    try:
        prediction_path = Path(args.prediction)
        config_path = Path(args.race_config)
        if prediction_path.exists() and config_path.exists():
            written = archive_prediction(load_json(prediction_path), load_json(config_path), Path(args.archive_dir), now)
            if written is not None:
                LOGGER.info("Archived pre-session prediction: %s", written)
        record = evaluate_archive(Path(args.archive_dir), Path(args.raw_dir))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    except Exception as exc:
        LOGGER.error("track_record failed: %s", exc)
        return 1
    LOGGER.info("Track record: %s", record["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
