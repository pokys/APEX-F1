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
import statistics
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.prediction_targeting import session_has_classification  # noqa: E402
from pipeline.openf1_client import OpenF1Client, format_duration  # noqa: E402
from pipeline.select_next_gp import extract_sessions_schedule  # noqa: E402

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
# Ergast/Jolpica provides no classification for practice and sprint
# qualifying. FastF1 can only derive SQ results from timing data and never
# classifies practice, so these sessions always need lap data loaded.
LAP_DATA_SESSIONS = {"FP1", "FP2", "FP3", "SQ"}
PRACTICE_SESSIONS = {"FP1", "FP2", "FP3"}
# Results of a finished weekend can still change (stewards' decisions,
# disqualifications). Sessions are re-fetched until the event is this many
# days older than the cutoff; afterwards a complete stored copy is reused.
REUSE_AFTER_DAYS = 3
SCHEDULE_BACKENDS = ("fastf1", "f1timing", "ergast")
SCHEDULE_RETRIES = 3
SCHEDULE_RETRY_BASE_SECONDS = 2.0


class ScheduleUnavailableError(RuntimeError):
    """Raised when none of the configured schedule backends respond."""


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
        default="FP1,FP2,FP3,SQ,S,Q,R",
        help="Comma-separated session codes (default: FP1,FP2,FP3,SQ,S,Q,R). Allowed: FP1,FP2,FP3,SQ,S,Q,R.",
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
        "--include-lap-metrics",
        action="store_true",
        help="Extract clean-lap pace metrics. Slower, but can improve prediction quality when used for targeted refreshes.",
    )
    parser.add_argument(
        "--no-openf1",
        action="store_true",
        help="Disable the OpenF1 fallback for practice/sprint qualifying classification.",
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
    text = str(value).strip().lower()
    return text in {"", "nan", "nat", "none"}


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


def to_json_scalar(value: Any) -> Any:
    if is_na_like(value):
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
        if is_na_like(value):
            return None
        return to_json_scalar(value)

    if isinstance(value, datetime):
        try:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()
        except Exception:
            return None
    if isinstance(value, date):
        return value.isoformat()

    return str(value)


def normalize_position(value: Any) -> int | None:
    scalar = to_json_scalar(value)
    if scalar is None:
        return None
    try:
        return int(float(str(scalar)))
    except (TypeError, ValueError):
        return None


def duration_seconds(value: Any) -> float | None:
    scalar = to_json_scalar(value)
    if scalar is None:
        return None
    if isinstance(scalar, (int, float)):
        number = float(scalar)
        return number if math.isfinite(number) else None
    text = str(scalar).strip()
    if not text:
        return None
    days = 0
    for marker in (" days ", " day "):
        if marker in text:
            prefix, _, rest = text.partition(marker)
            try:
                days = int(prefix)
                text = rest
            except ValueError:
                pass
            break
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return days * 86400.0 + float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
        if len(parts) == 2:
            return days * 86400.0 + float(parts[0]) * 60.0 + float(parts[1])
        return float(text)
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


def session_laps(session: Any) -> Any:
    """Return session.laps, or None when FastF1 could not load timing data.
    FastF1 raises DataNotLoadedError (not AttributeError) in that case."""
    try:
        return getattr(session, "laps", None)
    except Exception as exc:
        LOGGER.debug("Lap data unavailable: %s", exc)
        return None


def extract_lap_metrics(session: Any) -> list[dict[str, Any]]:
    laps = session_laps(session)
    if laps is None or getattr(laps, "empty", True):
        return []

    rows_by_driver: dict[str, dict[str, Any]] = {}
    for _, row in laps.iterrows():
        lap_time = duration_seconds(row.get("LapTime"))
        if lap_time is None or lap_time <= 0:
            continue
        if row.get("IsAccurate") is False:
            continue
        if str(row.get("Deleted") or "").strip().lower() in {"true", "1"}:
            continue
        if not is_na_like(row.get("PitInTime")) or not is_na_like(row.get("PitOutTime")):
            continue
        track_status = str(row.get("TrackStatus") or "")
        if any(flag in track_status for flag in ("4", "5", "6", "7")):
            continue

        driver = str(row.get("Driver") or "").strip().upper()
        if not driver:
            continue
        team = str(row.get("Team") or "").strip()
        state = rows_by_driver.setdefault(driver, {"driver": driver, "team": team, "lap_times": []})
        if team and not state.get("team"):
            state["team"] = team
        state["lap_times"].append(lap_time)

    if not rows_by_driver:
        return []

    median_by_driver = {
        driver: statistics.median(state["lap_times"])
        for driver, state in rows_by_driver.items()
        if state["lap_times"]
    }
    best_by_driver = {
        driver: min(state["lap_times"])
        for driver, state in rows_by_driver.items()
        if state["lap_times"]
    }
    best_median = min(median_by_driver.values()) if median_by_driver else None
    best_lap = min(best_by_driver.values()) if best_by_driver else None

    metrics: list[dict[str, Any]] = []
    for driver in sorted(rows_by_driver):
        state = rows_by_driver[driver]
        median_time = median_by_driver.get(driver)
        best_time = best_by_driver.get(driver)
        if median_time is None or best_time is None:
            continue
        metrics.append(
            {
                "abbreviation": driver,
                "team_name": state.get("team") or "",
                "clean_lap_count": len(state["lap_times"]),
                "median_clean_lap_time": round(median_time, 6),
                "best_clean_lap_time": round(best_time, 6),
                "pace_gap_to_best_seconds": round(median_time - best_median, 6) if best_median is not None else None,
                "best_lap_gap_to_best_seconds": round(best_time - best_lap, 6) if best_lap is not None else None,
            }
        )
    return metrics


def best_lap_seconds_by_driver(session: Any) -> dict[str, float]:
    """Fastest non-deleted lap per driver abbreviation."""
    laps = session_laps(session)
    if laps is None or getattr(laps, "empty", True):
        return {}
    best: dict[str, float] = {}
    for _, row in laps.iterrows():
        lap_time = duration_seconds(row.get("LapTime"))
        if lap_time is None or lap_time <= 0:
            continue
        if str(row.get("Deleted") or "").strip().lower() in {"true", "1"}:
            continue
        driver = str(row.get("Driver") or "").strip().upper()
        if not driver:
            continue
        if driver not in best or lap_time < best[driver]:
            best[driver] = lap_time
    return best


def classify_by_best_lap(records: list[dict[str, Any]], best_laps: dict[str, float]) -> list[dict[str, Any]]:
    """Fill positions from fastest laps when the session has no official
    classification (practice, or SQ when FastF1 could not compute it).

    Drivers without a timed lap keep position None. Existing positions are
    never overwritten."""
    if not records or not best_laps:
        return records
    if any(record.get("position") is not None for record in records):
        for record in records:
            abbr = str(record.get("abbreviation") or "").strip().upper()
            if abbr in best_laps and record.get("best_lap_seconds") is None:
                record["best_lap_seconds"] = round(best_laps[abbr], 6)
        return records

    timed: list[tuple[float, str, dict[str, Any]]] = []
    for record in records:
        abbr = str(record.get("abbreviation") or "").strip().upper()
        if abbr in best_laps:
            timed.append((best_laps[abbr], abbr, record))
    timed.sort(key=lambda item: (item[0], item[1]))
    for position, (seconds, _, record) in enumerate(timed, start=1):
        record["position"] = position
        record["best_lap_seconds"] = round(seconds, 6)
        if record.get("time") is None:
            record["time"] = format_duration(seconds)
        record["position_source"] = "best_lap"

    records.sort(
        key=lambda x: (
            x["position"] if x.get("position") is not None else 999,
            str(x.get("abbreviation") or ""),
            str(x.get("driver_number") or ""),
        )
    )
    return records


def load_session(season: int, round_number: int, session_code: str, cutoff: date, include_lap_metrics: bool = False) -> dict[str, Any] | None:
    try:
        session = fastf1.get_session(season, round_number, session_code)
    except Exception as exc:
        LOGGER.debug("Session lookup failed (%s round %s %s): %s", season, round_number, session_code, exc)
        return None

    session_date = to_utc_date(getattr(session, "date", None))
    if session_date and session_date > cutoff:
        return None

    load_laps = include_lap_metrics or session_code in LAP_DATA_SESSIONS
    # Race control messages mark deleted laps; FastF1 needs them to compute
    # a correct sprint qualifying classification.
    load_messages = session_code == "SQ"
    try:
        session.load(laps=load_laps, telemetry=False, weather=False, messages=load_messages)
    except TypeError:
        # Compatibility with older FastF1 versions.
        session.load(laps=load_laps, telemetry=False, weather=False)
    except Exception as exc:
        LOGGER.warning("Session load failed (%s round %s %s): %s", season, round_number, session_code, exc)
        return None

    results = extract_results(session)
    if not results:
        return None
    if load_laps and session_code in LAP_DATA_SESSIONS:
        results = classify_by_best_lap(results, best_lap_seconds_by_driver(session))
    if not session_has_classification(results):
        LOGGER.warning(
            "Session %s round %s %s has an entry list but no classification yet; skipping.",
            season,
            round_number,
            session_code,
        )
        return None

    payload = {
        "session_code": session_code,
        "session_name": to_json_scalar(getattr(session, "name", None)),
        "session_date": to_json_scalar(getattr(session, "date", None)),
        "results": results,
    }
    if load_laps:
        lap_metrics = extract_lap_metrics(session)
        if lap_metrics:
            payload["lap_metrics"] = lap_metrics
    return payload


def fetch_schedule(
    season: int,
    fetcher: Callable[[int, str], Any],
    backends: Iterable[str] = SCHEDULE_BACKENDS,
    retries: int = SCHEDULE_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    base_delay_seconds: float = SCHEDULE_RETRY_BASE_SECONDS,
) -> Any:
    """Fetch the season schedule, trying each backend with exponential backoff.

    The upstream F1 timing API and Ergast both fail intermittently. Without
    retry+fallback a single transient hiccup kills the whole pipeline run.
    Returns the schedule object on success; raises ScheduleUnavailableError
    when every backend has been exhausted so callers can decide whether to
    soft-fail (keep last good snapshot) or hard-fail.
    """
    last_exc: Exception | None = None
    for backend in backends:
        for attempt in range(1, max(1, retries) + 1):
            try:
                return fetcher(season, backend)
            except Exception as exc:
                last_exc = exc
                LOGGER.warning(
                    "Schedule fetch failed (season=%s, backend=%s, attempt=%s/%s): %s",
                    season,
                    backend,
                    attempt,
                    retries,
                    exc,
                )
                if attempt < retries:
                    sleep_fn(base_delay_seconds * (2 ** (attempt - 1)))
    raise ScheduleUnavailableError(
        f"No schedule backend responded for season {season} after {retries} attempts each: {last_exc}"
    )


def _default_schedule_fetcher(season: int, backend: str) -> Any:
    if fastf1 is None:
        raise RuntimeError("fastf1 is not installed.")
    return fastf1.get_event_schedule(season, include_testing=False, backend=backend)


def load_session_openf1(
    client: OpenF1Client,
    season: int,
    session_code: str,
    scheduled_start: Any,
) -> dict[str, Any] | None:
    """Fallback for practice/sprint qualifying when FastF1 has no timing
    data: take OpenF1 session results, or rank fastest laps."""
    try:
        session_key = client.find_session_key(season, session_code, scheduled_start)
        if session_key is None:
            return None
        results = client.session_results(session_key, session_code)
        if not session_has_classification(results):
            best = client.best_laps(session_key)
            if results:
                results = classify_by_best_lap(results, best)
            else:
                results = classify_by_best_lap(
                    [{"abbreviation": abbr, "position": None, "time": None, "source": "openf1"} for abbr in sorted(best)],
                    best,
                )
    except Exception as exc:
        LOGGER.warning("OpenF1 fallback failed (%s %s): %s", season, session_code, exc)
        return None
    if not session_has_classification(results):
        return None
    return {
        "session_code": session_code,
        "session_name": None,
        "session_date": str(scheduled_start),
        "source": "openf1",
        "openf1_session_key": session_key,
        "results": results,
    }


def align_team_names(results: list[dict[str, Any]], roster: dict[str, str]) -> list[dict[str, Any]]:
    """Use the FastF1 team name of each driver so a fallback source with
    different naming ("Red Bull Racing" vs "Red Bull") does not create a
    second team in features and ratings."""
    for row in results:
        abbr = str(row.get("abbreviation") or "").strip().upper()
        if abbr in roster:
            row["team_name"] = roster[abbr]
    return results


def update_roster(roster: dict[str, str], session: dict[str, Any]) -> None:
    if session.get("source") == "openf1":
        return
    for row in session.get("results") or []:
        if not isinstance(row, dict):
            continue
        abbr = str(row.get("abbreviation") or "").strip().upper()
        team = str(row.get("team_name") or "").strip()
        if abbr and team:
            roster[abbr] = team


def load_previous_sessions(snapshot_path: Path) -> dict[tuple[int, str], dict[str, Any]]:
    """Index sessions of an existing snapshot by (round, session_code)."""
    if not snapshot_path.exists():
        return {}
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Could not read previous snapshot %s: %s", snapshot_path, exc)
        return {}
    out: dict[tuple[int, str], dict[str, Any]] = {}
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        round_number = normalize_position(event.get("round"))
        if round_number is None:
            continue
        for session in event.get("sessions") or []:
            if not isinstance(session, dict):
                continue
            code = str(session.get("session_code") or "").strip().upper()
            if code:
                out[(round_number, code)] = session
    return out


def reusable_session(
    previous: dict[str, Any] | None,
    session_code: str,
    event_date: date | None,
    cutoff: date,
    include_lap_metrics: bool,
) -> bool:
    """A stored session can be reused instead of downloading it again when it
    is fully classified, carries the lap data this run would extract, and the
    event is old enough that results are final."""
    if not isinstance(previous, dict):
        return False
    if event_date is None or (cutoff - event_date).days < REUSE_AFTER_DAYS:
        return False
    if not session_has_classification(previous.get("results")):
        return False
    needs_laps = include_lap_metrics or session_code in LAP_DATA_SESSIONS
    if needs_laps and not previous.get("lap_metrics") and previous.get("source") != "openf1":
        return False
    return True


def ingest(
    season: int,
    sessions: list[str],
    cutoff: date,
    output_dir: Path,
    cache_dir: Path,
    include_lap_metrics: bool = False,
    openf1: OpenF1Client | None = None,
) -> Path | None:
    if fastf1 is None:
        raise RuntimeError("fastf1 is not installed. Install dependencies from requirements.txt first.")

    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    fastf1.Cache.enable_cache(str(cache_dir))
    try:
        schedule = fetch_schedule(season, fetcher=_default_schedule_fetcher)
    except ScheduleUnavailableError as exc:
        LOGGER.warning(
            "Keeping previous FastF1 snapshot for season %s: %s",
            season,
            exc,
        )
        return None
    schedule = schedule.sort_values(by=["EventDate", "RoundNumber"], kind="stable")
    previous_sessions = load_previous_sessions(output_dir / f"season_{season}.json")
    # Latest FastF1 team name per driver, seeded from the previous snapshot
    # so OpenF1 fallback sessions early in a weekend already map correctly.
    roster: dict[str, str] = {}
    for _, stored in sorted(previous_sessions.items(), key=lambda item: item[0][0]):
        update_roster(roster, stored)

    calendar_payload: list[dict[str, Any]] = []
    for _, row in schedule.iterrows():
        round_number = normalize_position(row.get("RoundNumber"))
        if round_number is None:
            continue
        event_date = to_utc_date(row.get("EventDate"))
        calendar_payload.append(
            {
                "round": round_number,
                "event_name": to_json_scalar(row.get("EventName")),
                "official_event_name": to_json_scalar(row.get("OfficialEventName")),
                "event_format": to_json_scalar(row.get("EventFormat")),
                "country": to_json_scalar(row.get("Country")),
                "location": to_json_scalar(row.get("Location")),
                "event_date": event_date.isoformat() if event_date else None,
                "sessions_schedule": extract_sessions_schedule(row),
            }
        )

    events_payload: list[dict[str, Any]] = []
    for _, row in schedule.iterrows():
        round_number = normalize_position(row.get("RoundNumber"))
        if round_number is None:
            continue

        event_date = to_utc_date(row.get("EventDate"))
        schedule_times = extract_sessions_schedule(row)
        sessions_payload: list[dict[str, Any]] = []
        for session_code in sessions:
            stored = previous_sessions.get((round_number, session_code))
            if reusable_session(stored, session_code, event_date, cutoff, include_lap_metrics):
                sessions_payload.append(stored)
                update_roster(roster, stored)
                continue
            loaded = load_session(season, round_number, session_code, cutoff=cutoff, include_lap_metrics=include_lap_metrics)
            if loaded is None and openf1 is not None and session_code in LAP_DATA_SESSIONS:
                scheduled = schedule_times.get(session_code)
                scheduled_dt = to_utc_date(scheduled)
                if scheduled and scheduled_dt is not None and scheduled_dt <= cutoff:
                    loaded = load_session_openf1(openf1, season, session_code, scheduled)
                    if loaded is not None:
                        align_team_names(loaded["results"], roster)
            if loaded is not None:
                update_roster(roster, loaded)
                sessions_payload.append(loaded)

        if not sessions_payload:
            continue

        events_payload.append(
            {
                "round": round_number,
                "event_name": to_json_scalar(row.get("EventName")),
                "official_event_name": to_json_scalar(row.get("OfficialEventName")),
                "event_format": to_json_scalar(row.get("EventFormat")),
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
        "calendar": calendar_payload,
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
            include_lap_metrics=args.include_lap_metrics,
            openf1=None if args.no_openf1 else OpenF1Client(),
        )
    except Exception as exc:
        LOGGER.error("ingest_fastf1 failed: %s", exc)
        return 1

    if output_path is None:
        # Schedule backends were unavailable. The previous snapshot remains
        # in place; downstream steps (build_features, select_next_gp) read
        # from cached calendar/snapshot, so the pipeline can keep going.
        LOGGER.warning(
            "FastF1 schedule unavailable; existing snapshot left untouched. "
            "Next scheduled run will retry."
        )
        return 0

    LOGGER.info("Wrote FastF1 snapshot: %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
