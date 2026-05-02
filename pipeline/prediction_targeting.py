#!/usr/bin/env python3
"""
Shared helpers for automatic prediction target selection and source manifests.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TARGET_SESSION_CODE = {
    "qualifying": "Q",
    "race": "R",
    "sprint_qualifying": "SQ",
    "sprint": "S",
}

TARGET_LABEL = {
    "qualifying": "Qualifying",
    "race": "Race",
    "sprint_qualifying": "Sprint Qualifying",
    "sprint": "Sprint",
}

TARGET_OUTPUT_TYPE = {
    "qualifying": "qualifying",
    "race": "race",
    "sprint_qualifying": "qualifying",
    "sprint": "race",
}

TARGET_SOURCE_MAP = {
    "qualifying": {
        "history_driver": "history_driver",
        "history_team": "history_team",
        "fp1": "FP1",
        "fp2": "FP2",
        "fp3": "FP3",
        "signals": "signals",
    },
    "race": {
        "history_driver": "history_driver",
        "history_team": "history_team",
        "qualifying": "Q",
        "fp2": "FP2",
        "fp3": "FP3",
        "signals": "signals",
    },
    "sprint_qualifying": {
        "history_driver": "history_driver",
        "history_team": "history_team",
        "fp1": "FP1",
        "signals": "signals",
    },
    "sprint": {
        "history_driver": "history_driver",
        "history_team": "history_team",
        "sprint_qualifying": "SQ",
        "fp1": "FP1",
        "signals": "signals",
    },
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_session_weights(path: Path) -> dict[str, dict[str, float]]:
    if not path.exists():
        return {}
    raw = load_json(path)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for target, weights in raw.items():
        if not isinstance(target, str) or not isinstance(weights, dict):
            continue
        clean: dict[str, float] = {}
        for source, value in weights.items():
            try:
                clean[str(source)] = float(value)
            except (TypeError, ValueError):
                continue
        out[target] = clean
    return out

def load_cached_calendar(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = load_json(path)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def find_cached_calendar_entry(calendar: list[dict[str, Any]], race_name: str) -> dict[str, Any] | None:
    key = race_name.strip().lower()
    for item in calendar:
        if str(item.get("event_name") or "").strip().lower() == key:
            return item
    return None
def normalize_weekend_format(event_format: Any, available_sessions: list[str]) -> str:
    text = str(event_format or "").strip().lower()
    if "sprint" in text:
        return "sprint"
    if "SQ" in available_sessions or "S" in available_sessions:
        return "sprint"
    return "standard"


DEFAULT_SESSION_COMPLETION_BUFFER_MINUTES = 90


def _parse_iso_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sessions_completed_by_calendar(
    sessions_schedule: dict[str, Any] | None,
    reference_time: Any,
    buffer_minutes: float = DEFAULT_SESSION_COMPLETION_BUFFER_MINUTES,
) -> list[str]:
    """Return session codes whose scheduled start is at least `buffer_minutes`
    in the past relative to `reference_time`.

    F1 sessions are 60 minutes (FP/Q/SQ) up to ~120 minutes (Race). We use
    a single configurable buffer that defaults to 90 minutes — long enough
    that a session is reliably finished, short enough that the next session
    isn't usually wrongly considered finished too. This is the calendar-time
    fallback used when FastF1 hasn't ingested the actual session results
    yet (upstream lag, transient outages, schedule re-numbering after a race
    cancellation, ...). It guarantees the prediction target advances on
    schedule even when hard-data ingest is delayed.
    """
    if not isinstance(sessions_schedule, dict) or not sessions_schedule:
        return []
    reference = _parse_iso_datetime(reference_time)
    if reference is None:
        return []
    buffer = timedelta(minutes=max(0.0, float(buffer_minutes)))
    completed: list[str] = []
    for code, value in sessions_schedule.items():
        scheduled_start = _parse_iso_datetime(value)
        if scheduled_start is None:
            continue
        if scheduled_start + buffer <= reference:
            code_upper = str(code).strip().upper()
            if code_upper:
                completed.append(code_upper)
    return completed


def select_prediction_target(weekend_format: str, available_sessions: list[str]) -> str:
    available = {str(code).upper() for code in available_sessions}
    if weekend_format == "sprint":
        if "Q" in available:
            return "race"
        if "S" in available:
            return "qualifying"
        if "SQ" in available:
            return "sprint"
        return "sprint_qualifying"
    if "Q" in available:
        return "race"
    return "qualifying"


def find_event(snapshot: dict[str, Any], event_name: str) -> dict[str, Any] | None:
    events = snapshot.get("events")
    if not isinstance(events, list):
        return None
    key = event_name.strip().lower()
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("event_name") or "").strip().lower() == key:
            return event
    return None


def find_calendar_entry(snapshot: dict[str, Any], event_name: str) -> dict[str, Any] | None:
    calendar = snapshot.get("calendar")
    if not isinstance(calendar, list):
        return None
    key = event_name.strip().lower()
    for event in calendar:
        if not isinstance(event, dict):
            continue
        if str(event.get("event_name") or "").strip().lower() == key:
            return event
    return None


def available_sessions_for_event(event: dict[str, Any]) -> list[str]:
    sessions = event.get("sessions")
    if not isinstance(sessions, list):
        return []
    available: list[str] = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        results = session.get("results")
        if not isinstance(results, list) or not results:
            continue
        code = str(session.get("session_code") or "").upper().strip()
        if code and code not in available:
            available.append(code)
    return available


def extract_fixed_grid_from_event(event: dict[str, Any], session_code: str) -> list[str] | None:
    sessions = event.get("sessions")
    if not isinstance(sessions, list):
        return None
    code_key = session_code.upper().strip()
    target_results: list[dict[str, Any]] | None = None
    for session in sessions:
        if not isinstance(session, dict):
            continue
        if str(session.get("session_code") or "").upper() != code_key:
            continue
        results = session.get("results")
        if isinstance(results, list) and results:
            target_results = [row for row in results if isinstance(row, dict)]
            break
    if not target_results:
        return None

    ranked: list[tuple[int, str]] = []
    for row in target_results:
        try:
            position = int(float(row.get("position")))
        except (TypeError, ValueError):
            continue
        name = str(row.get("abbreviation") or row.get("full_name") or "").strip().upper()
        if name:
            ranked.append((position, name))
    if not ranked:
        return None
    ranked.sort()
    return [name for _, name in ranked]


def signal_count(signals_dir: Path) -> int:
    if not signals_dir.exists():
        return 0
    total = 0
    for path in sorted(signals_dir.glob("*.json")):
        try:
            raw = load_json(path)
        except Exception:
            continue
        if isinstance(raw, list):
            total += len(raw)
        elif isinstance(raw, dict):
            signals = raw.get("signals")
            if isinstance(signals, list):
                total += len(signals)
            else:
                total += 1
    return total


def build_inputs_manifest(
    target: str,
    available_sessions: list[str],
    session_weights: dict[str, dict[str, float]],
    active_signal_count: int,
) -> list[dict[str, Any]]:
    weight_map = session_weights.get(target, {})
    source_map = TARGET_SOURCE_MAP.get(target, {})
    available = {str(code).upper() for code in available_sessions}

    active: list[tuple[str, str, float]] = []
    for source_name, configured_weight in weight_map.items():
        source_key = source_map.get(source_name)
        if not source_key:
            continue
        if source_key == "signals":
            if active_signal_count <= 0:
                continue
        elif source_key in {"history_driver", "history_team"}:
            pass
        elif source_key not in available:
            continue
        active.append((source_name, source_key, float(configured_weight)))

    total = sum(weight for _, _, weight in active)
    if total <= 0:
        return []

    manifest: list[dict[str, Any]] = []
    for source_name, source_key, weight in active:
        manifest.append(
            {
                "source": source_name,
                "source_key": source_key,
                "weight": round(weight / total, 6),
            }
        )
    manifest.sort(key=lambda item: (-float(item["weight"]), str(item["source"])))
    return manifest


def build_inputs_status(
    target: str,
    available_sessions: list[str],
    session_weights: dict[str, dict[str, float]],
    active_signal_count: int,
) -> list[dict[str, Any]]:
    weight_map = session_weights.get(target, {})
    source_map = TARGET_SOURCE_MAP.get(target, {})
    available = {str(code).upper() for code in available_sessions}
    used_manifest = build_inputs_manifest(
        target=target,
        available_sessions=available_sessions,
        session_weights=session_weights,
        active_signal_count=active_signal_count,
    )
    used_sources = {str(item.get("source")) for item in used_manifest}

    status_rows: list[dict[str, Any]] = []
    for source_name, source_key in source_map.items():
        configured_weight = float(weight_map.get(source_name, 0.0))
        if source_name in used_sources:
            status = "used"
        elif source_key in {"history_driver", "history_team"}:
            status = "available_zero_weight" if configured_weight <= 0 else "missing"
        elif source_key == "signals":
            if configured_weight <= 0:
                status = "available_zero_weight"
            elif active_signal_count <= 0:
                status = "missing"
            else:
                status = "available_zero_weight"
        elif source_key not in available:
            status = "not_applicable" if configured_weight <= 0 else "missing"
        else:
            status = "available_zero_weight"
        status_rows.append(
            {
                "source": source_name,
                "source_key": source_key,
                "configured_weight": round(configured_weight, 6),
                "status": status,
            }
        )
    status_rows.sort(key=lambda item: (str(item["status"]), str(item["source"])))
    return status_rows


def _parse_iso_date(raw: Any) -> date | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def latest_completed_event(snapshot: dict[str, Any]) -> tuple[str | None, date | None]:
    """Return (event_name, event_date) for the most recent event with at least
    one session that has non-empty results. Falls back to (None, None)."""
    events = snapshot.get("events")
    if not isinstance(events, list):
        return None, None
    best_name: str | None = None
    best_date: date | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        sessions = event.get("sessions")
        if not isinstance(sessions, list):
            continue
        has_results = False
        for session in sessions:
            if not isinstance(session, dict):
                continue
            results = session.get("results")
            if isinstance(results, list) and results:
                has_results = True
                break
        if not has_results:
            continue
        event_date = _parse_iso_date(event.get("event_date"))
        if event_date is None:
            continue
        if best_date is None or event_date > best_date:
            best_date = event_date
            best_name = str(event.get("event_name") or "").strip() or None
    return best_name, best_date


def compute_data_freshness(
    snapshot: dict[str, Any],
    race_date: Any,
    generated_at: Any,
    stale_threshold_days: float = 21.0,
) -> dict[str, Any]:
    """Build a freshness block describing the gap between the most recent
    completed event in the snapshot and now / the upcoming race.

    The output is always JSON-serialisable. is_stale is True when no event has
    any results, or when the gap exceeds stale_threshold_days. Used by the
    publish step and validate_outputs to surface stale-data warnings without
    breaking the pipeline.
    """
    threshold = float(stale_threshold_days) if stale_threshold_days is not None else 21.0
    if threshold < 0:
        threshold = 0.0

    name, latest_date = latest_completed_event(snapshot)

    reference: date | None = None
    if isinstance(generated_at, str) and generated_at:
        try:
            reference = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).astimezone(timezone.utc).date()
        except ValueError:
            reference = _parse_iso_date(generated_at)
    if reference is None:
        reference = datetime.now(timezone.utc).date()

    next_race_date = _parse_iso_date(race_date)

    days_since = None
    if latest_date is not None:
        days_since = (reference - latest_date).days

    days_until = None
    if next_race_date is not None:
        days_until = (next_race_date - reference).days

    is_stale = (latest_date is None) or (days_since is not None and days_since > threshold)

    return {
        "latest_completed_event": name,
        "latest_completed_date": latest_date.isoformat() if latest_date else None,
        "reference_date": reference.isoformat(),
        "next_race_date": next_race_date.isoformat() if next_race_date else None,
        "days_since_latest_event": days_since,
        "days_until_next_race": days_until,
        "stale_threshold_days": round(threshold, 6),
        "is_stale": bool(is_stale),
    }


def session_position_score(event: dict[str, Any], session_code: str, driver_name: str) -> float | None:
    sessions = event.get("sessions")
    if not isinstance(sessions, list):
        return None
    code_key = session_code.upper().strip()
    for session in sessions:
        if not isinstance(session, dict):
            continue
        if str(session.get("session_code") or "").upper() != code_key:
            continue
        results = session.get("results")
        if not isinstance(results, list) or not results:
            return None
        grid_size = len([row for row in results if isinstance(row, dict)])
        if grid_size <= 1:
            return None
        for row in results:
            if not isinstance(row, dict):
                continue
            name = str(row.get("abbreviation") or row.get("full_name") or "").strip().upper()
            if name != driver_name.upper():
                continue
            try:
                pos = int(float(row.get("position")))
            except (TypeError, ValueError):
                return None
            base = (grid_size - pos) / (grid_size - 1)
            if code_key in {"Q", "SQ"}:
                depth = 0
                if row.get("q1") is not None:
                    depth += 1
                if row.get("q2") is not None:
                    depth += 1
                if row.get("q3") is not None:
                    depth += 1
                phase_score = depth / 3.0
                return 0.7 * base + 0.3 * phase_score
            return base
    return None


def compute_weekend_form(driver_name: str, event: dict[str, Any], manifest: list[dict[str, Any]]) -> dict[str, Any]:
    weighted_sum = 0.0
    total_weight = 0.0
    source_rows: list[dict[str, Any]] = []

    for item in manifest:
        source = str(item.get("source_key") or "")
        weight = float(item.get("weight") or 0.0)
        if weight <= 0:
            continue

        # Historical and signal-derived inputs act as a neutral baseline.
        # Session results then pull the score away from that baseline according
        # to their configured share of the active manifest.
        if source in {"history_driver", "history_team", "signals"}:
            weighted_sum += weight * 0.5
            total_weight += weight
            continue

        score = session_position_score(event, source, driver_name)
        if score is None:
            continue
        weighted_sum += weight * score
        total_weight += weight
        source_rows.append(
            {
                "session": source,
                "weight": round(weight, 6),
                "score": round(score, 6),
            }
        )

    if total_weight <= 0:
        return {"delta": 0.0, "sources": source_rows}

    normalized_score = weighted_sum / total_weight
    # Convert 0..1 session form into a bounded rating delta.
    delta = (normalized_score - 0.5) * 10.0
    return {
        "delta": round(max(-5.0, min(5.0, delta)), 6),
        "sources": source_rows,
    }
