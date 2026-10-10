#!/usr/bin/env python3
"""
Minimal OpenF1 (https://openf1.org) client used as a fallback data source.

FastF1 derives practice and sprint qualifying classifications from the F1
live timing API. When that API has no data for a session, OpenF1 still
publishes session results, driver lists, laps and race control messages.
Only the standard library is used so the pipeline needs no extra
dependency.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

LOGGER = logging.getLogger("openf1_client")

BASE_URL = "https://api.openf1.org/v1"
REQUEST_PAUSE_SECONDS = 0.4
REQUEST_RETRIES = 3
SESSION_MATCH_TOLERANCE = timedelta(hours=6)

SESSION_NAMES = {
    "FP1": "Practice 1",
    "FP2": "Practice 2",
    "FP3": "Practice 3",
    "SQ": "Sprint Qualifying",
    "S": "Sprint",
    "Q": "Qualifying",
    "R": "Race",
}

Fetcher = Callable[[str, dict[str, Any]], Any]


def _http_fetch(endpoint: str, params: dict[str, Any]) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE_URL}/{endpoint}" + (f"?{query}" if query else "")
    last_exc: Exception | None = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        time.sleep(REQUEST_PAUSE_SECONDS)
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed https host
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 404:
                return []
            if exc.code == 429:
                time.sleep(2.0 * attempt)
                continue
            if 500 <= exc.code < 600:
                time.sleep(1.0 * attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            time.sleep(1.0 * attempt)
    raise RuntimeError(f"OpenF1 request failed for {url}: {last_exc}")


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_duration(seconds: float) -> str:
    total = max(0.0, float(seconds))
    days = int(total // 86400)
    rest = total - days * 86400
    hours = int(rest // 3600)
    rest -= hours * 3600
    minutes = int(rest // 60)
    rest -= minutes * 60
    return f"{days} days {hours:02d}:{minutes:02d}:{rest:09.6f}"


def _status_from_position(position: Any) -> str | None:
    """OpenF1 puts codes such as "RT" (retired) in the position field of
    drivers without a classified result."""
    if isinstance(position, str) and position.strip() and not position.strip().isdigit():
        return {"RT": "DNF"}.get(position.strip().upper(), position.strip().upper())
    return None


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class OpenF1Client:
    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or _http_fetch
        self._sessions_by_year: dict[int, list[dict[str, Any]]] = {}

    def get(self, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        payload = self._fetch(endpoint, params)
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def sessions(self, year: int) -> list[dict[str, Any]]:
        if year not in self._sessions_by_year:
            self._sessions_by_year[year] = self.get("sessions", year=year)
        return self._sessions_by_year[year]

    def find_session_key(self, year: int, session_code: str, scheduled_start: Any) -> int | None:
        """Match an OpenF1 session by name and start time (within a few hours
        of the scheduled UTC start)."""
        name = SESSION_NAMES.get(session_code.upper())
        start = _parse_dt(scheduled_start)
        if not name or start is None:
            return None
        best: tuple[timedelta, int] | None = None
        for row in self.sessions(year):
            if str(row.get("session_name") or "").strip().lower() != name.lower():
                continue
            row_start = _parse_dt(row.get("date_start"))
            key = row.get("session_key")
            if row_start is None or key is None:
                continue
            distance = abs(row_start - start)
            if distance <= SESSION_MATCH_TOLERANCE and (best is None or distance < best[0]):
                best = (distance, int(key))
        return best[1] if best else None

    def session_results(self, session_key: int, session_code: str) -> list[dict[str, Any]]:
        """Results in the snapshot's FastF1-like row format."""
        drivers = {
            int(row["driver_number"]): row
            for row in self.get("drivers", session_key=session_key)
            if row.get("driver_number") is not None
        }
        quali_like = session_code.upper() in {"Q", "SQ"}
        records: list[dict[str, Any]] = []
        for row in self.get("session_result", session_key=session_key):
            number = row.get("driver_number")
            if number is None:
                continue
            info = drivers.get(int(number), {})
            position = row.get("position")
            record: dict[str, Any] = {
                "position": int(position) if isinstance(position, (int, float)) else None,
                "grid_position": None,
                "driver_number": str(number),
                "abbreviation": info.get("name_acronym"),
                "full_name": info.get("full_name"),
                "team_name": info.get("team_name"),
                "classified_position": str(int(position)) if isinstance(position, (int, float)) else None,
                "status": "DNF" if row.get("dnf") else ("DNS" if row.get("dns") else ("DSQ" if row.get("dsq") else _status_from_position(position))),
                "points": None,
                "time": None,
                "q1": None,
                "q2": None,
                "q3": None,
                "source": "openf1",
            }
            duration = row.get("duration")
            if quali_like and isinstance(duration, list):
                for idx, key in enumerate(("q1", "q2", "q3")):
                    seconds = _as_float(duration[idx]) if idx < len(duration) else None
                    if seconds is not None:
                        record[key] = format_duration(seconds)
            else:
                seconds = _as_float(duration)
                if seconds is not None:
                    record["time"] = format_duration(seconds)
                    if session_code.upper().startswith("FP"):
                        record["best_lap_seconds"] = round(seconds, 6)
            records.append(record)
        records.sort(key=lambda x: (x["position"] if x["position"] is not None else 999, str(x.get("abbreviation") or "")))
        return records

    def best_laps(self, session_key: int) -> dict[str, float]:
        """Fastest lap per driver acronym, for sessions without results."""
        drivers = {
            int(row["driver_number"]): str(row.get("name_acronym") or "").upper()
            for row in self.get("drivers", session_key=session_key)
            if row.get("driver_number") is not None
        }
        best: dict[str, float] = {}
        for lap in self.get("laps", session_key=session_key):
            seconds = _as_float(lap.get("lap_duration"))
            abbr = drivers.get(int(lap.get("driver_number") or 0))
            if seconds is None or not abbr:
                continue
            if abbr not in best or seconds < best[abbr]:
                best[abbr] = seconds
        return best

    def session_is_wet(self, session_key: int) -> bool | None:
        """True when a meaningful share of drivers ran intermediate or wet
        tyres, or rain was recorded. None when OpenF1 has no data."""
        stints = self.get("stints", session_key=session_key)
        if stints:
            drivers = {row.get("driver_number") for row in stints if row.get("driver_number") is not None}
            wet_drivers = {
                row.get("driver_number")
                for row in stints
                if str(row.get("compound") or "").upper() in {"INTERMEDIATE", "WET"}
            }
            if drivers:
                return len(wet_drivers) / len(drivers) >= 0.25
        weather = self.get("weather", session_key=session_key)
        if weather:
            return any(float(row.get("rainfall") or 0) > 0 for row in weather)
        return None

    def race_control(self, session_key: int) -> list[dict[str, Any]]:
        return self.get("race_control", session_key=session_key)
