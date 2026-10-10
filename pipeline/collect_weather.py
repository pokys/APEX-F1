#!/usr/bin/env python3
"""
Rain forecast for every session of the upcoming GP.

Uses the free Open-Meteo forecast API (no key) with the circuit coordinates
from config/circuits.json and the exact session start times from
config/race_config.json. For each session the highest hourly precipitation
probability and the summed precipitation over the session window are
stored in outputs/weather_forecast.json. The dashboard shows them and
mixes the dry and wet scenario by the chance of a wet session.

Best effort: a missing circuit, a session beyond the forecast horizon or a
network failure never fails the pipeline.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.prediction_targeting import _parse_iso_datetime, load_json  # noqa: E402

LOGGER = logging.getLogger("collect_weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# Open-Meteo forecasts up to 16 days ahead.
FORECAST_HORIZON_DAYS = 15
SESSION_DURATION_MINUTES = {"FP1": 60, "FP2": 60, "FP3": 60, "SQ": 45, "S": 40, "Q": 60, "R": 120}
# A session counts as wet on the timeline when its estimated wet share
# (wet_session_probability) reaches this value.
WET_SCENARIO_THRESHOLD = 0.5

# For the dry/wet mix: this much rain over a session counts as a fully wet
# session; less rain scales the wet share down (a 90 % chance of 0.2 mm is
# a damp track for a few minutes, not a wet race).
WET_TRACK_MM = 1.0
# Open-Meteo's precipitation amounts are unreliable for tropical showers and
# thunderstorms (Singapore 2026 sprint: 100 % chance, 0.0 mm, heavy rain).
# The WMO weather code of the session window and a near-certain probability
# therefore set a floor on how much of the probability counts as wet.
STORM_CODES = {95, 96, 99}
HEAVY_RAIN_CODES = {65, 67, 82}
MODERATE_RAIN_CODES = {63, 66, 81}
CERTAIN_RAIN_PROBABILITY = 0.9


def wet_factor_floor(weather_code: Any, probability: float) -> float:
    try:
        code = int(weather_code)
    except (TypeError, ValueError):
        code = None
    floor = 0.0
    if code in STORM_CODES or code in HEAVY_RAIN_CODES:
        floor = 0.8
    elif code in MODERATE_RAIN_CODES:
        floor = 0.5
    if probability >= CERTAIN_RAIN_PROBABILITY:
        floor = max(floor, 0.5)
    return floor

Fetcher = Callable[[str, dict[str, Any]], Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect the rain forecast for the upcoming GP sessions.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config JSON.")
    parser.add_argument("--circuits", default="config/circuits.json", help="Circuit coordinates JSON.")
    parser.add_argument("--output", default="outputs/weather_forecast.json", help="Forecast output JSON.")
    parser.add_argument("--now", default=None, help="UTC ISO time used as 'now' (default: current time).")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def http_fetch(url: str, params: dict[str, Any], attempts: int = 3) -> Any:
    query = urllib.parse.urlencode(params)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(f"{url}?{query}", timeout=30) as response:  # noqa: S310 - fixed https host
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # network errors, timeouts, HTTP errors
            last_exc = exc
            LOGGER.warning("Weather request attempt %s/%s failed: %s", attempt, attempts, exc)
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"Open-Meteo unavailable: {last_exc}")


def circuit_coordinates(circuits: dict[str, Any], race_name: str) -> tuple[float, float] | None:
    by_name = circuits.get("by_event_name") if isinstance(circuits, dict) else None
    if not isinstance(by_name, dict):
        return None
    key = race_name.strip().lower()
    for name, coords in by_name.items():
        if str(name).strip().lower() == key and isinstance(coords, dict):
            try:
                return float(coords["lat"]), float(coords["lon"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def hourly_series(payload: Any) -> list[tuple[datetime, float | None, float | None, int | None]]:
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        return []
    times = hourly.get("time") or []
    probabilities = hourly.get("precipitation_probability") or []
    amounts = hourly.get("precipitation") or []
    codes = hourly.get("weather_code") or []
    series = []
    for idx, raw_time in enumerate(times):
        parsed = _parse_iso_datetime(raw_time)
        if parsed is None:
            continue
        probability = probabilities[idx] if idx < len(probabilities) else None
        amount = amounts[idx] if idx < len(amounts) else None
        code = codes[idx] if idx < len(codes) else None
        series.append((parsed, probability, amount, code))
    return series


def session_forecast(series: list[tuple], start: datetime, minutes: int) -> dict[str, Any] | None:
    """Worst hour of the session window: max precipitation probability,
    summed precipitation and the most severe WMO weather code over the hours
    that overlap the session."""
    end = start + timedelta(minutes=minutes)
    window = [entry for entry in series if entry[0] < end and entry[0] + timedelta(hours=1) > start]
    probabilities = [float(entry[1]) for entry in window if entry[1] is not None]
    amounts = [float(entry[2]) for entry in window if entry[2] is not None]
    codes = [int(entry[3]) for entry in window if len(entry) > 3 and entry[3] is not None]
    if not probabilities and not amounts:
        return None
    forecast: dict[str, Any] = {
        "rain_probability": round(max(probabilities) / 100.0, 3) if probabilities else None,
        "precipitation_mm": round(sum(amounts), 2) if amounts else None,
    }
    if codes:
        forecast["weather_code"] = max(codes)
    return forecast


def build_forecast(race_config: dict[str, Any], circuits: dict[str, Any], fetch: Fetcher, now: datetime) -> dict[str, Any] | None:
    race_name = str(race_config.get("race") or "")
    coords = circuit_coordinates(circuits, race_name)
    schedule = race_config.get("sessions_schedule") if isinstance(race_config.get("sessions_schedule"), dict) else {}
    if coords is None or not schedule:
        LOGGER.warning("No coordinates or schedule for %r; skipping weather forecast.", race_name)
        return None
    starts = {code: _parse_iso_datetime(value) for code, value in schedule.items()}
    starts = {code: start for code, start in starts.items() if start is not None}
    horizon = now + timedelta(days=FORECAST_HORIZON_DAYS)
    in_range = {code: start for code, start in starts.items() if start + timedelta(hours=3) >= now and start <= horizon}
    if not in_range:
        return None
    first = min(in_range.values()).date()
    last = (max(in_range.values()) + timedelta(hours=3)).date()
    payload = fetch(
        FORECAST_URL,
        {
            "latitude": coords[0],
            "longitude": coords[1],
            "hourly": "precipitation_probability,precipitation,weather_code",
            "timezone": "UTC",
            "start_date": first.isoformat(),
            "end_date": last.isoformat(),
        },
    )
    series = hourly_series(payload)
    sessions: dict[str, Any] = {}
    for code, start in sorted(in_range.items(), key=lambda item: item[1]):
        forecast = session_forecast(series, start, SESSION_DURATION_MINUTES.get(code.upper(), 60))
        if forecast is not None:
            sessions[code.upper()] = {"start": start.isoformat(), **forecast}
    if not sessions:
        return None
    return {
        "season": race_config.get("season"),
        "race": race_name,
        "longitude": coords[1],
        "fetched_at": now.replace(microsecond=0).isoformat(),
        "source": "open-meteo.com",
        "sessions": sessions,
    }


def wet_session_probability(info: dict[str, Any] | None) -> float | None:
    """Estimated chance that the session runs in wet conditions: the rain
    probability scaled down when only a little rain is expected, unless the
    weather code (showers, thunderstorm) or a near-certain probability says
    otherwise."""
    if not isinstance(info, dict) or info.get("rain_probability") is None:
        return None
    probability = max(0.0, min(1.0, float(info["rain_probability"])))
    amount = info.get("precipitation_mm")
    factor = 1.0 if amount is None else min(1.0, max(0.0, float(amount)) / WET_TRACK_MM)
    factor = max(factor, wet_factor_floor(info.get("weather_code"), probability))
    return round(probability * factor, 3)


def weather_icon(info: dict[str, Any] | None, start: datetime | None = None, longitude: float | None = None) -> tuple[str, str]:
    """Emoji and a short word for a session's forecast. Clear sky after
    dark (local solar time from the longitude) is a moon: Singapore and Las
    Vegas race at night."""
    if not isinstance(info, dict) or info.get("rain_probability") is None:
        return "", ""
    probability = float(info["rain_probability"])
    share = wet_session_probability(info) or 0.0
    try:
        code = int(info.get("weather_code"))
    except (TypeError, ValueError):
        code = None
    if code in STORM_CODES:
        return "\u26c8\ufe0f", "Thunderstorm"
    if share >= WET_SCENARIO_THRESHOLD:
        return "\U0001f327\ufe0f", "Wet likely"
    if share >= 0.15 or probability >= 0.5:
        return "\U0001f326\ufe0f", "Showers possible"
    night = False
    if start is not None and longitude is not None:
        local_hour = (start.hour + start.minute / 60 + longitude / 15.0) % 24
        night = local_hour >= 18.5 or local_hour < 6  # dusk ~18:30 local solar time
    if probability >= 0.2:
        return ("\u2601\ufe0f", "Mostly dry") if night else ("\u26c5", "Mostly dry")
    return ("\U0001f319", "Dry") if night else ("\u2600\ufe0f", "Dry")


def is_wet_session(info: dict[str, Any] | None) -> bool:
    share = wet_session_probability(info)
    return share is not None and share >= WET_SCENARIO_THRESHOLD


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")
    now = _parse_iso_datetime(args.now) if args.now else datetime.now(timezone.utc)
    output = Path(args.output)
    try:
        race_config = load_json(Path(args.race_config))
        circuits = load_json(Path(args.circuits)) if Path(args.circuits).exists() else {}
        forecast = build_forecast(race_config, circuits, http_fetch, now)
    except Exception as exc:
        LOGGER.warning("Weather forecast skipped: %s", exc)
        return 0
    if forecast is None:
        LOGGER.info("No weather forecast available for the upcoming sessions.")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(forecast, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    LOGGER.info("Wrote weather forecast for %s sessions: %s", len(forecast["sessions"]), output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
