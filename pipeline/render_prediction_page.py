#!/usr/bin/env python3
"""
Render a static HTML dashboard from target-aware prediction output.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.collect_weather import is_wet_session, weather_icon, wet_session_probability  # noqa: E402
from pipeline.prediction_history import phase_changes  # noqa: E402


LOGGER = logging.getLogger("render_prediction_page")

TEAM_COLORS = {
    "red bull": "#3671C6",
    "mercedes": "#27F4D2",
    "ferrari": "#E80020",
    "mclaren": "#FF8000",
    "aston martin": "#229971",
    "alpine": "#0093CC",
    "williams": "#64C4FF",
    "haas": "#B6BABD",
    "racing bulls": "#6692FF",
    "rb": "#6692FF",
    "sauber": "#52E252",
    "audi": "#52E252",
    "default": "#9bb0c6",
}

QUALIFYING_TARGETS = {"qualifying", "sprint_qualifying"}
# Smallest change (in percentage points) shown as a move up or down.
DELTA_THRESHOLD_PP = 0.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render prediction dashboard HTML.")
    parser.add_argument("--prediction", default="outputs/prediction.json", help="Single prediction JSON input path.")
    parser.add_argument("--prediction-dry", default="outputs/prediction_dry.json", help="Dry scenario prediction JSON input path.")
    parser.add_argument("--prediction-wet", default="outputs/prediction_wet.json", help="Wet scenario prediction JSON input path.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config JSON input path.")
    parser.add_argument("--tyres-input", default="data/raw/tyres", help="Weekend tyre compounds file or directory.")
    parser.add_argument("--track-record", default="outputs/track_record.json", help="Track record JSON (optional).")
    parser.add_argument("--weather", default="outputs/weather_forecast.json", help="Weather forecast JSON (optional).")
    parser.add_argument("--history", default="outputs/prediction_history.json", help="Prediction phase history JSON (optional).")
    parser.add_argument("--archive-dir", default="outputs/archive", help="Archived pre-session predictions (optional).")
    parser.add_argument("--raw-dir", default="data/raw/fastf1", help="FastF1 snapshots for this weekend's session results (optional).")
    parser.add_argument("--output", default="outputs/prediction_report.html", help="Rendered HTML output path.")
    parser.add_argument("--allow-missing-input", action="store_true", help="Exit 0 if prediction input is missing.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def slug(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


def load_tyre_compounds(tyres_input: Path, season: int, race_name: str) -> dict[str, Any] | None:
    path = tyres_input / f"season_{season}.json" if tyres_input.is_dir() else tyres_input
    if not path.exists():
        return None
    try:
        raw = load_json(path)
    except Exception:
        return None
    if not isinstance(raw, list):
        return None
    target = slug(race_name)
    for row in raw:
        if not isinstance(row, dict):
            continue
        if slug(str(row.get("event_name") or "")) == target:
            return row
    return None


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def get_team_color(team_name: str) -> str:
    cleaned = str(team_name or "").strip().lower()
    for key, color in TEAM_COLORS.items():
        if key in cleaned:
            return color
    return TEAM_COLORS["default"]


def parse_prediction_rows(prediction: dict[str, Any]) -> list[dict[str, Any]]:
    target = str(prediction.get("prediction_target") or "race")
    drivers = prediction.get("drivers")
    if not isinstance(drivers, list) or not drivers:
        raise ValueError("Prediction JSON missing non-empty 'drivers' list.")

    rows: list[dict[str, Any]] = []
    for raw in drivers:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        row = {
            "name": name,
            "team": str(raw.get("team") or "Unknown"),
            "driver_share": to_float(raw.get("driver_share"), 50.0),
            "team_share": to_float(raw.get("team_share"), 50.0),
            "weekend_form_delta": to_float(raw.get("weekend_form_delta"), 0.0),
            "position_probabilities": [to_float(v, 0.0) for v in raw.get("position_probabilities") or []],
            "dnf_probability": to_float(raw.get("dnf_probability"), -1.0) if raw.get("dnf_probability") is not None else None,
        }
        if target in QUALIFYING_TARGETS:
            row["headline_probability"] = max(0.0, min(1.0, to_float(raw.get("pole_probability"), 0.0)))
            row["secondary_probability"] = max(0.0, min(1.0, to_float(raw.get("front_row_probability"), 0.0)))
            row["third_probability"] = max(0.0, min(1.0, to_float(raw.get("top10_probability"), 0.0)))
            row["expected_metric"] = max(1.0, to_float(raw.get("expected_position"), 99.0))
        else:
            row["headline_probability"] = max(0.0, min(1.0, to_float(raw.get("win_probability"), 0.0)))
            row["secondary_probability"] = max(0.0, min(1.0, to_float(raw.get("podium_probability"), 0.0)))
            row["third_probability"] = 0.0
            row["expected_metric"] = max(1.0, to_float(raw.get("expected_finish"), 99.0))
        rows.append(row)

    rows.sort(key=lambda item: (-item["headline_probability"], item["expected_metric"], item["name"].lower()))
    return rows


def manifest_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<div class="empty-card">No active weighted inputs.</div>'
    cards = []
    for item in items:
        source = html.escape(str(item.get("source") or "unknown"))
        key = html.escape(str(item.get("source_key") or ""))
        weight = to_float(item.get("weight"), 0.0) * 100.0
        cards.append(
            '<article class="input-card">'
            f'<p class="input-source">{source}</p>'
            f'<p class="input-key">{key}</p>'
            f'<div class="input-bar"><span style="width:{weight:.2f}%"></span></div>'
            f'<p class="input-weight">{weight:.2f}%</p>'
            "</article>"
        )
    return "".join(cards)


def input_status_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<div class="empty-card">No input status available.</div>'
    cards = []
    labels = {
        "used": "Used",
        "missing": "Missing",
        "pending_ingest": "Pending ingest",
        "not_applicable": "Not applicable",
        "available_zero_weight": "Available, zero weight",
    }
    for item in items:
        source = html.escape(str(item.get("source") or "unknown"))
        key = html.escape(str(item.get("source_key") or ""))
        status = str(item.get("status") or "unknown")
        status_label = labels.get(status, status.replace("_", " ").title())
        weight = to_float(item.get("configured_weight"), 0.0) * 100.0
        cards.append(
            f'<article class="status-input-card status-{html.escape(status)}">'
            f'<p class="input-source">{source}</p>'
            f'<p class="input-key">{key}</p>'
            f'<p class="status-badge">{html.escape(status_label)}</p>'
            f'<p class="input-weight">Configured weight: {weight:.2f}%</p>'
            "</article>"
        )
    return "".join(cards)


def metric_labels(target: str) -> tuple[str, str, str]:
    if target in QUALIFYING_TARGETS:
        return ("Pole", "Front Row", "Top 10")
    return ("Win", "Podium", "Expected")


def weather_detail_text(info: dict[str, Any]) -> str:
    """'Rain chance 100% · 0.2 mm · wet session ~50%' for the click-open detail."""
    parts = [f"Rain chance {float(info.get('rain_probability') or 0.0) * 100:.0f}%"]
    if info.get("precipitation_mm") is not None:
        parts.append(f"{float(info['precipitation_mm']):.1f} mm expected")
    share = wet_session_probability(info)
    if share is not None:
        parts.append(f"wet session ~{share * 100:.0f}%")
    return " &middot; ".join(parts)


def weather_chip_html(info: dict[str, Any], start: datetime | None, longitude: float | None) -> str:
    icon, word = weather_icon(info, start, longitude)
    if not icon:
        return ""
    wet_class = " rain-high" if is_wet_session(info) else ""
    return (
        f'<details class="wx{wet_class}"><summary title="{html.escape(word)} (click for details)">'
        f'<span class="wx-icon" aria-hidden="true">{icon}</span><span class="wx-word">{html.escape(word)}</span></summary>'
        f'<span class="wx-detail">{weather_detail_text(info)}<br>Open-Meteo forecast</span></details>'
    )


def timeline_html(
    weekend_format: str,
    available_sessions: list[str],
    target_session_code: str,
    sessions_schedule: dict[str, Any] | None = None,
    weather: dict[str, Any] | None = None,
    live: bool = False,
) -> str:
    """Weekend sessions in chronological order with their start (shown in
    the visitor's local time by a small script, UTC as fallback), status,
    rain forecast and a countdown to the next session."""
    schedule: dict[str, datetime] = {}
    for code, value in (sessions_schedule or {}).items():
        start = parse_utc(value)
        if start is not None:
            schedule[str(code).upper()] = start
    default_steps = ["FP1", "SQ", "S", "Q", "R"] if weekend_format == "sprint" else ["FP1", "FP2", "FP3", "Q", "R"]
    steps = sorted(schedule, key=lambda code: schedule[code]) if schedule else default_steps
    available = {str(code).upper() for code in available_sessions}
    rain = {}
    longitude = None
    if isinstance(weather, dict):
        try:
            longitude = float(weather["longitude"]) if weather.get("longitude") is not None else None
        except (TypeError, ValueError):
            longitude = None
        rain = {str(code).upper(): info for code, info in (weather.get("sessions") or {}).items() if isinstance(info, dict)}

    cards = []
    next_code = None
    for step in steps:
        if step in available:
            status, label = "done", "completed"
        elif step == target_session_code.upper():
            status, label = ("live", "live · prediction locked") if live else ("current", "predicting now")
        else:
            status, label = "upcoming", "upcoming"
        if next_code is None and step not in available and not (live and step == target_session_code.upper()):
            next_code = step
        start = schedule.get(step)
        time_html = ""
        if start is not None:
            iso = start.isoformat()
            time_html = f'<time datetime="{iso}" data-local-time="{iso}">{start.strftime("%a %H:%M")} UTC</time>'
        info = rain.get(step, {})
        rain_html = weather_chip_html(info, start, longitude) if status != "done" else ""
        cards.append(
            f'<article class="timeline-step timeline-{status}">'
            f'<p>{html.escape(SESSION_NAMES.get(step, step))}</p>{time_html}<span>{html.escape(label)}</span>{rain_html}</article>'
        )
    countdown = ""
    if next_code and next_code in schedule:
        countdown = (
            f'<p class="next-session" data-countdown="{schedule[next_code].isoformat()}">'
            f'Next: <strong>{html.escape(SESSION_NAMES.get(next_code, next_code))}</strong> '
            f'<span class="countdown-value">{schedule[next_code].strftime("%a %d %b %H:%M")} UTC</span></p>'
        )
    return countdown + f'<section class="timeline-grid">{"".join(cards)}</section>'


SESSION_NAMES = {
    "FP1": "Practice 1",
    "FP2": "Practice 2",
    "FP3": "Practice 3",
    "SQ": "Sprint Qualifying",
    "S": "Sprint",
    "Q": "Qualifying",
    "R": "Race",
}


def parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def why_active_now(target: str, weekend_format: str, available_sessions: list[str]) -> str:
    available = {str(code).upper() for code in available_sessions}
    if target == "sprint_qualifying":
        return "This is a sprint weekend and no sprint qualifying result is available yet, so the system is forecasting Sprint Qualifying."
    if target == "sprint":
        return "Sprint Qualifying is already available, so the system now switches to the Sprint itself."
    if target == "qualifying" and weekend_format == "sprint":
        if "S" in available:
            return "The sprint has already been run, and the next competitive session is Qualifying for the main Grand Prix."
        return "The next competitive session is Qualifying, so the system is estimating the starting order."
    if target == "qualifying":
        return "Qualifying has not been completed yet, so the system is estimating the next starting order."
    if target == "race":
        return "Qualifying results are available, so the system now simulates the race from the known grid when possible."
    return "The system is automatically selecting the next competitive session and forecasting it."


def season_blend_note(season_blend: dict[str, Any]) -> str:
    current_weight = int(to_float(season_blend.get("current_weight"), 100))
    current_season = int(to_float(season_blend.get("current_season"), 0))
    previous_weight = int(to_float(season_blend.get("previous_weight"), 0))
    previous_season = int(to_float(season_blend.get("previous_season"), 0))
    if current_weight >= 100 or previous_weight <= 0 or current_season <= 0 or previous_season <= 0:
        return ""
    return (
        f"Because the season is still young, the model is blending {current_weight}% of {current_season} data "
        f"with {previous_weight}% of {previous_season} data."
    )


def odds_bar_html(row: dict[str, Any], target: str) -> str:
    """Nested bars on one track: the darkest segment is the headline
    probability (pole / win), lighter ones the wider outcomes (front row and
    top 10 / podium). Each segment starts at zero, so lengths read directly."""
    qualifying = target in QUALIFYING_TARGETS
    layers = [("l1", row["headline_probability"])]
    layers.append(("l2", row["secondary_probability"]))
    if qualifying:
        layers.append(("l3", row["third_probability"]))
    labels = ("Pole", "Front row", "Top 10") if qualifying else ("Win", "Podium")
    title = " · ".join(f"{label} {value * 100:.1f}%" for label, (_, value) in zip(labels, layers))
    spans = "".join(
        f'<span class="odds-{name}" style="width:{max(0.0, min(1.0, value)) * 100:.2f}%"></span>'
        for name, value in reversed(layers)
    )
    return f'<div class="odds-bar" style="--team-color: {get_team_color(row["team"])}" title="{html.escape(title)}" role="img" aria-label="{html.escape(title)}">{spans}</div>'


def delta_html(name: str, changes: dict[str, Any] | None) -> str:
    if not isinstance(changes, dict):
        return ""
    delta = (changes.get("deltas") or {}).get(name)
    if delta is None:
        return ""
    points = delta * 100.0
    label = html.escape(str(changes.get("label") or "previous phase"))
    if abs(points) < DELTA_THRESHOLD_PP:
        return f'<span class="delta delta-flat" title="No change {label}">&ndash;</span>'
    arrow, css = (ARROW_UP, "delta-up") if points > 0 else (ARROW_DOWN, "delta-down")
    return f'<span class="delta {css}" title="{points:+.1f} pp {label}">{arrow} {abs(points):.1f}</span>'


# Below / above these wet shares the mix is shown as the plain dry / wet
# scenario instead.
MIX_MIN_WET_SHARE = 0.03
MIX_MAX_WET_SHARE = 0.97


def blend_predictions(dry: dict[str, Any], wet: dict[str, Any], wet_share: float) -> dict[str, Any]:
    """Per-driver mix of the dry and wet scenario weighted by the chance of
    a wet session. Probabilities and expected positions mix linearly, so
    the result is still a consistent distribution."""
    wet_rows = {str(row.get("name")): row for row in wet.get("drivers") or [] if isinstance(row, dict)}
    drivers = []
    for row in dry.get("drivers") or []:
        if not isinstance(row, dict):
            continue
        other = wet_rows.get(str(row.get("name")), row)
        mixed = dict(row)
        for key, value in row.items():
            if key in {"driver_share", "team_share", "weekend_form_delta"}:
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(other.get(key), (int, float)):
                mixed[key] = (1.0 - wet_share) * value + wet_share * float(other[key])
            elif isinstance(value, list) and isinstance(other.get(key), list) and len(other[key]) == len(value):
                mixed[key] = [(1.0 - wet_share) * float(a) + wet_share * float(b) for a, b in zip(value, other[key])]
        drivers.append(mixed)
    return {**dry, "drivers": drivers}


def blend_changes(dry: dict[str, Any] | None, wet: dict[str, Any] | None, wet_share: float) -> dict[str, Any] | None:
    if not isinstance(dry, dict) or not isinstance(wet, dict):
        return dry
    names = set(dry.get("deltas") or {}) | set(wet.get("deltas") or {})
    deltas = {
        name: (1.0 - wet_share) * (dry.get("deltas") or {}).get(name, 0.0) + wet_share * (wet.get("deltas") or {}).get(name, 0.0)
        for name in names
    }
    before = {
        name: (1.0 - wet_share) * (dry.get("before") or {}).get(name, 0.0) + wet_share * (wet.get("before") or {}).get(name, 0.0)
        for name in set(dry.get("before") or {}) | set(wet.get("before") or {})
    }
    return {**dry, "deltas": deltas, "before": before}


def penalty_badges(race_config: dict[str, Any] | None) -> dict[str, str]:
    """Short badges per driver code: grid penalties ("-25 grid") and
    substitutes ("sub for STR")."""
    badges: dict[str, list[str]] = {}
    # Outside the race itself, say which start a race penalty applies to.
    race_prefix = "" if str((race_config or {}).get("prediction_target") or "race") == "race" else "Race "
    for key, start in (("race_grid_penalties", race_prefix), ("sprint_grid_penalties", "Sprint ")):
        for penalty in (race_config or {}).get(key) or []:
            if not isinstance(penalty, dict) or not penalty.get("driver"):
                continue
            if penalty.get("excluded"):
                short, long = "out", "excluded"
            elif penalty.get("pit_lane"):
                short, long = "pit start", "starts from the pit lane"
            elif penalty.get("back_of_grid"):
                short, long = "back of grid", "starts from the back of the grid"
            else:
                places = int(to_float(penalty.get("places")))
                short, long = f"&minus;{places} grid", f"{places} place grid drop"
            what = "sprint" if key == "sprint_grid_penalties" else "race"
            badges.setdefault(str(penalty["driver"]).upper(), []).append(
                f'<span class="penalty-badge" title="{html.escape(what.capitalize())} grid penalty: {long}">{start}{short}</span>'
            )
    for sub in (race_config or {}).get("driver_substitutions") or []:
        if isinstance(sub, dict) and sub.get("driver_in"):
            replaced = html.escape(str(sub.get("driver_out") or ""))
            badges.setdefault(str(sub["driver_in"]).upper(), []).append(
                f'<span class="sub-badge" title="Substitute driver this weekend, replacing {replaced}">sub for {replaced}</span>'
            )
    return {driver: " ".join(items) for driver, items in badges.items()}


def position_histogram_svg(probabilities: list[float], color: str) -> str:
    """Bars for P1..PN, height proportional to the probability of finishing
    there; hovering a bar shows the value."""
    if not probabilities:
        return ""
    count = len(probabilities)
    bar_w, gap, height = 8, 2, 40
    width = count * (bar_w + gap)
    peak = max(max(probabilities), 1e-9)
    bars = []
    for idx, p in enumerate(probabilities):
        h = max(1.0, (p / peak) * (height - 2)) if p > 0 else 0.0
        x = idx * (bar_w + gap)
        bars.append(
            f'<rect x="{x}" y="{height - h:.1f}" width="{bar_w}" height="{h:.1f}" rx="2" fill="{color}">'
            f"<title>P{idx + 1}: {p * 100:.1f}%</title></rect>"
        )
    ticks = "".join(
        f'<text x="{(n - 1) * (bar_w + gap) + bar_w / 2}" y="{height + 11}" text-anchor="middle">P{n}</text>'
        for n in (1, 5, 10, 15, 20)
        if n <= count
    )
    return (
        f'<svg class="pos-hist" viewBox="0 0 {width} {height + 14}" role="img" aria-label="Finishing position distribution">'
        f'{"".join(bars)}<g class="pos-hist-ticks">{ticks}</g></svg>'
    )


def median_position(probabilities: list[float]) -> int | None:
    """Finishing position reached or beaten in half of the simulations."""
    cumulative = 0.0
    for idx, p in enumerate(probabilities, start=1):
        cumulative += p
        if cumulative >= 0.5:
            return idx
    return None


def reference_positions(
    target: str, race_config: dict[str, Any], weekend: dict[str, list[tuple[str, int]]]
) -> tuple[dict[str, int], str]:
    """Positions the predicted order is compared with, and their label: the
    starting grid (penalties included) for a sprint or race, otherwise the
    classification of the latest session of this weekend."""
    grid = race_config.get("fixed_grid") if target not in QUALIFYING_TARGETS else None
    if grid:
        return {str(name).upper(): idx for idx, name in enumerate(grid, start=1)}, "start"
    order = ("FP1", "FP2", "FP3", "SQ", "S", "Q", "R")
    sessions = {code for results in weekend.values() for code, _ in results}
    if not sessions:
        return {}, ""
    latest = max(sessions, key=order.index)
    return {name: pos for name, results in weekend.items() for code, pos in results if code == latest}, latest


# Inline SVG instead of the ▲▼ characters: iOS Safari draws those with a
# system symbol font that ignores the text colour.
ARROW_UP = '<svg class="tri" viewBox="0 0 10 10" aria-hidden="true"><path d="M5 1 9.5 9h-9z" fill="currentColor"/></svg>'
ARROW_DOWN = '<svg class="tri" viewBox="0 0 10 10" aria-hidden="true"><path d="M5 9 .5 1h9z" fill="currentColor"/></svg>'


def position_arrow_html(name: str, predicted: int | None, context: dict[str, Any] | None) -> str:
    """Places the driver is predicted to gain (green) or lose (red): the
    predicted finishing order (table rank) against the reference positions
    (start grid or latest session)."""
    context = context or {}
    reference = (context.get("reference") or {}).get(name.upper())
    label = context.get("reference_label") or ""
    if predicted is None or reference is None:
        return ""
    where = "the start" if label == "start" else label
    diff = reference - predicted
    title = f"Predicted P{predicted}, P{reference} at {where}"
    if diff == 0:
        return f'<span class="delta delta-flat" title="{html.escape(title)}">&ndash;</span>'
    arrow, css = (ARROW_UP, "delta-up") if diff > 0 else (ARROW_DOWN, "delta-down")
    return f'<span class="delta {css}" title="{html.escape(title)}">{arrow} {abs(diff)}</span>'


def driver_detail_html(row: dict[str, Any], qualifying: bool, context: dict[str, Any] | None) -> str:
    """Expandable detail of one driver: start vs expected result, DNF risk,
    the driver's sessions this weekend and the finishing distribution."""
    context = context or {}
    name = row["name"]
    facts = []
    grid_pos = (context.get("grid") or {}).get(name)
    probs = row.get("position_probabilities") or []
    if probs:
        # The mean is misleading for a two-peaked distribution (the standings
        # blend), so show the median and the single most likely position.
        median = median_position(probs) or len(probs)
        mode = max(range(len(probs)), key=lambda i: (probs[i], -i)) + 1
        outcome = f"<b>median</b> P{median} &middot; <b>most likely</b> P{mode} ({probs[mode - 1] * 100:.0f}%)"
        reference = median
    else:
        outcome = f"<b>expected</b> ~P{row['expected_metric']:.1f}"
        reference = row["expected_metric"]
    if grid_pos and not qualifying:
        trend = "up" if reference < grid_pos else "down" if reference > grid_pos else "flat"
        facts.append(f'<span class="fact"><b>Start</b> P{grid_pos} <span class="trend-{trend}">&rarr;</span> {outcome}</span>')
    else:
        facts.append(f'<span class="fact">{outcome}</span>')
    changes = context.get("changes") or {}
    before = (changes.get("before") or {}).get(name)
    if before is not None:
        sessions = "+".join(changes.get("new_sessions") or []) or "the latest session"
        chance = "Pole" if qualifying else "Win"
        now = row["headline_probability"]
        trend = "up" if now > before + 0.005 else "down" if now < before - 0.005 else "flat"
        facts.append(
            f'<span class="fact"><b>{chance} chance</b> {before * 100:.0f}% before {html.escape(sessions)} '
            f'<span class="trend-{trend}">&rarr;</span> {now * 100:.0f}% now</span>'
        )
    form = row.get("weekend_form_delta")
    if form:
        facts.append(f'<span class="fact"><b>Weekend form</b> {form:+.2f} rating</span>')
    dnf = row.get("dnf_probability")
    if not qualifying and dnf is not None and dnf >= 0:
        facts.append(f'<span class="fact"><b>DNF risk</b> {dnf * 100:.0f}%</span>')
    sessions = (context.get("weekend") or {}).get(name) or []
    if sessions:
        chips = "".join(f'<span class="session-chip">{html.escape(code)} P{pos}</span>' for code, pos in sessions)
        facts.append(f'<span class="fact"><b>This weekend</b> {chips}</span>')
    histogram = position_histogram_svg(row.get("position_probabilities") or [], get_team_color(row["team"]))
    hist_html = f'<div class="pos-hist-wrap"><span class="hist-label">Finishing position chances</span>{histogram}</div>' if histogram else ""
    return f'<div class="driver-detail-body"><div class="facts">{"".join(facts)}</div>{hist_html}</div>'


def weekend_session_results(snapshot: dict[str, Any] | None, round_number: Any) -> dict[str, list[tuple[str, int]]]:
    """Classified position of every driver in each session of this GP so far."""
    out: dict[str, list[tuple[str, int]]] = {}
    if not isinstance(snapshot, dict):
        return out
    try:
        target_round = int(round_number)
    except (TypeError, ValueError):
        return out
    order = {code: idx for idx, code in enumerate(("FP1", "FP2", "FP3", "SQ", "S", "Q", "R"))}
    for event in snapshot.get("events") or []:
        if not isinstance(event, dict) or to_float(event.get("round"), -1) != target_round:
            continue
        sessions = sorted(
            (s for s in event.get("sessions") or [] if isinstance(s, dict) and str(s.get("session_code") or "").upper() in order),
            key=lambda s: order[str(s.get("session_code")).upper()],
        )
        for session in sessions:
            code = str(session.get("session_code")).upper()
            for result in session.get("results") or []:
                abbr = str(result.get("abbreviation") or "").upper()
                pos = to_float(result.get("position"), 0)
                if abbr and pos >= 1:
                    out.setdefault(abbr, []).append((code, int(pos)))
    return out


def scenario_panel_html(
    prediction: dict[str, Any],
    scenario_key: str,
    scenario_label: str,
    active: bool,
    changes: dict[str, Any] | None = None,
    penalties: dict[str, str] | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    target = str(prediction.get("prediction_target") or "race")
    rows = parse_prediction_rows(prediction)
    for row in rows:
        row["median"] = median_position(row.get("position_probabilities") or [])
    # Predicted finishing order: median position, then the headline chance.
    rows.sort(key=lambda r: (r["median"] or 99, -r["headline_probability"], r["expected_metric"], r["name"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    primary_label, secondary_label, tertiary_label = metric_labels(target)
    qualifying = target in QUALIFYING_TARGETS
    penalties = penalties or {}
    context = dict(context or {}, changes=changes)
    reference_label = context.get("reference_label") or ""

    def arrow(row: dict[str, Any]) -> str:
        return position_arrow_html(row["name"], row["rank"], context)

    def predicted(row: dict[str, Any]) -> str:
        return f"P{row['median']}" if row["median"] else f"~{row['expected_metric']:.1f}"

    def badge(name: str) -> str:
        return (" " + penalties[name.upper()]) if name.upper() in penalties else ""

    top_cards = []
    for idx, row in enumerate(rows[:3], start=1):
        color = get_team_color(row["team"])
        top_cards.append(
            '<article class="hero-card" style="--team-color: {color}">'.format(color=color)
            + f'<p class="hero-rank">P{idx} {arrow(row)}</p>'
            + f'<h3>{html.escape(row["name"])}{badge(row["name"])}</h3>'
            + f'<p class="hero-team">{html.escape(row["team"])}</p>'
            + f'<p class="hero-big">{row["headline_probability"] * 100:.1f}%<small>{primary_label.lower()}</small></p>'
            + odds_bar_html(row, target)
            + f'<p class="hero-metric">{secondary_label}: {row["secondary_probability"] * 100:.1f}%</p>'
            + "</article>"
        )

    expected_label = "Predicted"
    table_rows = []
    for idx, row in enumerate(rows, start=1):
        color = get_team_color(row["team"])
        detail_id = f"detail-{scenario_key}-{html.escape(row['name'])}"
        numbers = f"{row['headline_probability'] * 100:.1f}% / {row['secondary_probability'] * 100:.1f}%"
        if qualifying:
            numbers += f" / {row['third_probability'] * 100:.1f}%"
        table_rows.append(
            '<tr style="--team-color: {color}">'.format(color=color)
            + f"<td>{idx}</td>"
            + f'<td><button class="detail-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}" title="Show driver detail">'
            + f'<strong>{html.escape(row["name"])}</strong><span class="caret" aria-hidden="true">&#9662;</span></button>{badge(row["name"])}<small>{html.escape(row["team"])}</small></td>'
            + f'<td class="odds-cell">{odds_bar_html(row, target)}<small>{numbers}</small></td>'
            + f"<td>{arrow(row)}</td>"
            + "</tr>"
            + f'<tr class="detail-row" id="{detail_id}" hidden><td colspan="4">{driver_detail_html(row, qualifying, context)}</td></tr>'
        )

    mobile_cards = []
    for row in rows:
        color = get_team_color(row["team"])
        mobile_cards.append(
            '<article class="mobile-driver-card" style="--team-color: {color}">'.format(color=color)
            + f'<div class="mobile-top"><h4><span class="rank">{row["rank"]}</span>{html.escape(row["name"])}{badge(row["name"])} {arrow(row)}</h4><span>{html.escape(row["team"])}</span></div>'
            + odds_bar_html(row, target)
            + f'<p>{primary_label} {row["headline_probability"] * 100:.1f}% · {secondary_label} {row["secondary_probability"] * 100:.1f}%'
            + (f' · {tertiary_label} {row["third_probability"] * 100:.1f}%' if qualifying else "")
            + "</p>"
            + f'<details class="driver-detail"><summary>Detail</summary>{driver_detail_html(row, qualifying, context)}</details>'
            + "</article>"
        )

    legend_items = [("l1", primary_label), ("l2", secondary_label)] + ([("l3", tertiary_label)] if qualifying else [])
    legend = "".join(f'<span class="legend-item"><i class="legend-{key}"></i>{html.escape(label)}</span>' for key, label in legend_items)
    where = "the start" if reference_label == "start" else reference_label
    change_note = f'<span class="legend-item"><span class="delta delta-up">{ARROW_UP}</span><span class="delta delta-down">{ARROW_DOWN}</span> places vs {html.escape(where)}</span>' if reference_label else ""
    change_header = f"vs {html.escape('start' if reference_label == 'start' else reference_label)}" if reference_label else "Change"
    odds_header = " / ".join(label for _, label in legend_items)
    active_class = " is-active" if active else ""
    return (
        f'<section class="scenario-panel{active_class}" data-scenario="{scenario_key}">'
        f'<section class="hero-grid">{"".join(top_cards)}</section>'
        f'<p class="odds-legend">{legend}{change_note}</p>'
        f'<section class="desktop-table"><table>'
        f"<thead><tr><th>#</th><th>Driver</th><th>{odds_header}</th><th>{change_header}</th></tr></thead>"
        f"<tbody>{''.join(table_rows)}</tbody></table></section>"
        f'<section class="mobile-list">{"".join(mobile_cards)}</section>'
        "</section>"
    )


HELP_HTML = """
<details class="page-help">
  <summary>How to read this page</summary>
  <dl>
    <dt>Order of the table</dt>
    <dd>Drivers are listed in the predicted finishing order: by the median simulated position (the position the driver
    reaches or beats in half of the simulations), ties broken by the win/pole chance.</dd>
    <dt>Bars and percentages</dt>
    <dd>Win / podium (race, sprint) or pole / front row / top 10 (qualifying): the share of simulations in which that
    happens. Bright part = the headline result, darker parts = the wider ones.</dd>
    <dt>__ARROW_UP__ / __ARROW_DOWN__ next to a driver</dt>
    <dd>Places the driver is predicted to gain (green) or lose (red): their place in the predicted order
    (the # column) against the last time they were classified &ndash; the starting grid for a sprint or race (penalties
    included), otherwise the latest session of this weekend (e.g. FP1 or the sprint). &ndash; means no change.</dd>
    <dt>Driver detail (click the name / "Detail")</dt>
    <dd>Start &rarr; median (the position reached or beaten in half of the simulations) and most likely position,
    weekend form, DNF risk, the driver's results in this weekend's sessions, the
    win/pole chance before the latest session and now (e.g. "Win chance 17% before FP1+SQ &rarr; 6% now": how much the latest sessions changed the
    prediction), and the chance of finishing in each position.</dd>
    <dt>Badges</dt>
    <dd>Red: grid penalty from the FIA stewards' decisions or race control (&minus;N grid, pit start, back of grid).
    Blue outline: substitute driver from the official entry list.</dd>
    <dt>Dry / Wet / Mix</dt>
    <dd>The model simulates a dry and a wet race. Mix weights them by the chance that the session runs in the wet,
    estimated from the Open-Meteo forecast (rain probability, expected amount and weather code). Weather icons:
    &#9728;&#65039;/&#127769; dry, &#9925; mostly dry, &#127782;&#65039; showers possible, &#127783;&#65039; wet likely,
    &#9928;&#65039; thunderstorm; click an icon for the numbers.</dd>
    <dt>Weekend form (driver detail)</dt>
    <dd>How much this weekend's sessions moved the driver's rating up or down.</dd>
    <dt>Accuracy</dt>
    <dd>"How accurate is it?" compares past predictions with the results and with a simple guess based on the
    championship order.</dd>
  </dl>
</details>
"""
HELP_HTML = HELP_HTML.replace("__ARROW_UP__", f'<span class="delta delta-up">{ARROW_UP}</span>').replace(
    "__ARROW_DOWN__", f'<span class="delta delta-down">{ARROW_DOWN}</span>'
)




def penalties_html(race_config: dict[str, Any]) -> str:
    """Grid penalties known for this GP and where they come from."""
    sections = []
    for key, label in (("race_grid_penalties", "Race"), ("sprint_grid_penalties", "Sprint")):
        rows = [p for p in race_config.get(key) or [] if isinstance(p, dict)]
        if not rows:
            continue
        items = []
        for penalty in rows:
            if penalty.get("excluded"):
                what = "excluded (race ban)"
            elif penalty.get("pit_lane"):
                what = "pit lane start"
            elif penalty.get("back_of_grid"):
                what = "back of the grid"
            else:
                what = f"{int(to_float(penalty.get('places')))} place grid drop"
            sources = " ".join(source_link_html(src) for src in penalty.get("sources") or [])
            items.append(
                f"<li><span><strong>{html.escape(str(penalty.get('driver') or ''))}</strong> &middot; {html.escape(what)}</span>"
                f'<span class="muted">{sources}</span></li>'
            )
        sections.append(f'<h3 class="subhead">{label}</h3><ul class="info-list">{"".join(items)}</ul>')
    if not sections:
        return (
            '<section class="card"><h2 class="card-title">Grid Penalties</h2>'
            '<p class="muted">No grid penalty is known for this GP (FIA stewards\' documents + race control).</p></section>'
        )
    return '<section class="card"><h2 class="card-title">Grid Penalties</h2>' + "".join(sections) + "</section>"


def source_link_html(src: Any) -> str:
    text = str(src or "")
    if not text.startswith("http"):
        return html.escape(text)
    label = "FIA document" if "fia.com" in text else "race control" if "openf1" in text else "source"
    return f'<a href="{html.escape(text)}" target="_blank" rel="noopener">{label} &#8599;</a>'


def track_record_html(track_record: dict[str, Any] | None) -> str:
    """Scores of archived pre-session predictions once results are in."""
    if not isinstance(track_record, dict):
        return ""
    summary = track_record.get("summary") if isinstance(track_record.get("summary"), dict) else {}
    entries = [row for row in track_record.get("entries") or [] if isinstance(row, dict)]
    if not summary.get("count"):
        return (
            '<section class="card"><h2 class="card-title">Track Record</h2>'
            '<p class="muted">No archived prediction has been scored yet. Predictions are archived before each session starts and scored once results are in.</p>'
            "</section>"
        )
    rows = []
    for row in entries[-12:][::-1]:
        hit = "&#10003;" if row.get("hit") else "&#10007;"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('season')))} R{html.escape(str(row.get('round')))}</td>"
            f"<td>{html.escape(str(row.get('race') or ''))}</td>"
            f"<td>{html.escape(str(row.get('session_code') or ''))}</td>"
            f"<td>{html.escape(str(row.get('predicted_winner') or ''))}</td>"
            f"<td>{html.escape(str(row.get('actual_winner') or ''))} {hit}</td>"
            f"<td>{to_float(row.get('winner_probability')) * 100:.1f}%</td>"
            "</tr>"
        )
    return (
        '<section class="card"><h2 class="card-title">Track Record</h2>'
        f"<p class=\"muted\">{int(summary.get('count', 0))} scored predictions &middot; favourite correct {to_float(summary.get('hit_rate')) * 100:.0f}% "
        f"&middot; mean probability on the actual winner {to_float(summary.get('mean_winner_probability')) * 100:.1f}% "
        f"&middot; mean log loss {to_float(summary.get('mean_log_loss')):.2f}</p>"
        '<div class="table-scroll"><table><thead><tr><th>Event</th><th>GP</th><th>Session</th><th>Favourite</th><th>Actual</th><th>P(actual)</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        "</section>"
    )


def render_page(
    prediction: dict[str, Any],
    race_config: dict[str, Any],
    prediction_wet: dict[str, Any] | None = None,
    tyre_compounds: dict[str, Any] | None = None,
    track_record: dict[str, Any] | None = None,
    weather: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    weekend_results: dict[str, list[tuple[str, int]]] | None = None,
    locked: dict[str, Any] | None = None,
) -> str:
    target = str(prediction.get("prediction_target") or race_config.get("prediction_target") or "race")
    target_theme = "quali" if target in QUALIFYING_TARGETS else "race"
    target_label = str(prediction.get("prediction_target_label") or race_config.get("prediction_target_label") or "Race")
    target_output_type = str(prediction.get("target_output_type") or race_config.get("target_output_type") or "race")
    race_name = html.escape(str(prediction.get("race") or race_config.get("race") or "Next GP"))
    generated_at = html.escape(str(prediction.get("generated_at") or race_config.get("generated_at") or ""))
    weekend_format = html.escape(str(prediction.get("weekend_format") or race_config.get("weekend_format") or "standard"))
    target_session_code = html.escape(str(prediction.get("target_session_code") or race_config.get("target_session_code") or "R"))
    lock_html = ""
    live = False
    if isinstance(locked, dict) and locked.get("drivers") and str(locked.get("session_code") or "").upper() == target_session_code.upper():
        # The target session is running: show the prediction archived just
        # before its start, which is what the track record will score.
        prediction = {**prediction, "drivers": locked["drivers"]}
        prediction_wet = None
        live = True
        session_name = html.escape(SESSION_NAMES.get(target_session_code, target_session_code))
        archived = html.escape(str(locked.get("archived_at") or ""))
        lock_html = (
            f'<p class="live-banner"><span class="live-dot" aria-hidden="true"></span><span><b>{session_name} is running.</b> '
            f'Showing the prediction locked at its start (<time data-local-time="{archived}">{archived}</time>); '
            "results replace it once the session is classified.</span></p>"
        )
    available_sessions = prediction.get("simulation", {}).get("available_sessions") or race_config.get("available_sessions") or []
    available_sessions_label = ", ".join(str(code) for code in available_sessions) if available_sessions else "none"
    inputs_used = prediction.get("inputs_used") or race_config.get("inputs_used") or []
    inputs_status = prediction.get("inputs_status") or race_config.get("inputs_status") or []
    season_blend = prediction.get("season_blend") or {}
    grid_source = html.escape(str(prediction.get("simulation", {}).get("grid_source") or race_config.get("grid_source") or "simulation"))
    simulations = int(to_float(prediction.get("simulation", {}).get("simulations"), to_float(race_config.get("simulations"), 0)))
    signal_count = int(to_float(race_config.get("signal_count"), 0))
    why_now = why_active_now(target, str(prediction.get("weekend_format") or race_config.get("weekend_format") or "standard"), list(available_sessions))
    blend_note = season_blend_note(season_blend if isinstance(season_blend, dict) else {})
    if blend_note:
        why_now = f"{why_now} {blend_note}"
    season_blend_summary = html.escape(str((season_blend or {}).get("summary") or "Unknown"))
    compounds_html = ""
    if isinstance(tyre_compounds, dict) and isinstance(tyre_compounds.get("compounds"), dict):
        compounds = tyre_compounds["compounds"]
        compounds_html = (
            '<section class="card tyre-card">'
            '<h2 class="card-title">Pirelli Weekend Compounds</h2>'
            '<div class="tyre-grid">'
            f'<article class="tyre-item tyre-hard"><p class="tyre-label">Hard</p><p class="tyre-value">{html.escape(str(compounds.get("hard") or "?"))}</p></article>'
            f'<article class="tyre-item tyre-medium"><p class="tyre-label">Medium</p><p class="tyre-value">{html.escape(str(compounds.get("medium") or "?"))}</p></article>'
            f'<article class="tyre-item tyre-soft"><p class="tyre-label">Soft</p><p class="tyre-value">{html.escape(str(compounds.get("soft") or "?"))}</p></article>'
            "</div>"
            "</section>"
        )

    if isinstance(weather, dict) and str(weather.get("race") or "").lower() != str(prediction.get("race") or race_config.get("race") or "").lower():
        weather = None
    session_info = ((weather or {}).get("sessions") or {}).get(str(target_session_code).upper()) if isinstance(weather, dict) else None
    rain_probability = session_info.get("rain_probability") if isinstance(session_info, dict) else None
    wet_share = wet_session_probability(session_info) if isinstance(prediction_wet, dict) else None
    if wet_share is None or wet_share < MIX_MIN_WET_SHARE:
        recommended = "dry"
    elif wet_share > MIX_MAX_WET_SHARE:
        recommended = "wet"
    else:
        recommended = "mixed"
    weather_banner = ""
    if rain_probability is not None:
        session_name = html.escape(SESSION_NAMES.get(str(target_session_code), str(target_session_code)))
        icon, word = weather_icon(session_info, parse_utc(session_info.get("start")), (weather or {}).get("longitude"))
        advice = "prediction locked at the start" if live else {
            "dry": "showing the dry scenario",
            "wet": "showing the wet scenario",
            "mixed": f"showing the dry/wet mix ({(wet_share or 0.0) * 100:.0f}% wet)",
        }.get(recommended, "")
        weather_banner = (
            f'<details class="weather-banner weather-{recommended}"><summary>'
            f'<span class="wx-icon" aria-hidden="true">{icon}</span> {session_name}: {html.escape(word.lower())} &middot; {advice}</summary>'
            f'<p>{weather_detail_text(session_info)} (Open-Meteo forecast). The dry and wet predictions are mixed by the '
            f"chance of a wet session; Dry / Wet above switch to the pure scenarios.</p></details>"
        )

    toggle_html = ""
    script_html = ""
    if isinstance(prediction_wet, dict):
        dry_active = " is-active" if recommended == "dry" else ""
        wet_active = " is-active" if recommended == "wet" else ""
        mixed_button = (
            f'<button class="toggle-btn is-active" data-target="mixed" type="button">Mix &middot; {(wet_share or 0.0) * 100:.0f}% wet</button>'
            if recommended == "mixed"
            else ""
        )
        toggle_html = (
            '<div class="scenario-toggle">'
            + mixed_button
            + f'<button class="toggle-btn{dry_active}" data-target="dry" type="button">Dry</button>'
            + f'<button class="toggle-btn{wet_active}" data-target="wet" type="button">Wet</button>'
            + "</div>"
        )
        script_html = """
    <script>
      const buttons = Array.from(document.querySelectorAll('.toggle-btn'));
      const panels = Array.from(document.querySelectorAll('.scenario-panel'));
      for (const button of buttons) {
        button.addEventListener('click', () => {
          const target = button.dataset.target;
          for (const other of buttons) {
            other.classList.toggle('is-active', other === button);
          }
          for (const panel of panels) {
            panel.classList.toggle('is-active', panel.dataset.scenario === target);
          }
        });
      }
    </script>
"""

    phase_config = {"season": prediction.get("season") or race_config.get("season"), "next_round": race_config.get("next_round")}
    badges = penalty_badges(race_config)
    fixed_grid = race_config.get("fixed_grid") if target not in QUALIFYING_TARGETS else None
    reference, reference_label = reference_positions(target, race_config, weekend_results or {})
    context = {
        "grid": {str(name).upper(): idx for idx, name in enumerate(fixed_grid or [], start=1)},
        "weekend": weekend_results or {},
        "reference": reference,
        "reference_label": reference_label,
    }
    dry_changes = phase_changes(history, phase_config, "dry")
    wet_changes = phase_changes(history, phase_config, "wet")
    dry_panel = scenario_panel_html(prediction, "dry", "Dry", recommended == "dry", dry_changes, badges, context)
    if recommended == "mixed" and isinstance(prediction_wet, dict):
        share = float(wet_share or 0.0)
        dry_panel = (
            scenario_panel_html(
                blend_predictions(prediction, prediction_wet, share),
                "mixed",
                f"Forecast mix ({(1 - share) * 100:.0f}% dry / {share * 100:.0f}% wet)",
                True,
                blend_changes(dry_changes, wet_changes, share),
                badges,
                context,
            )
            + dry_panel
        )
    wet_panel = (
        scenario_panel_html(prediction_wet, "wet", "Wet", recommended == "wet", wet_changes, badges, context)
        if isinstance(prediction_wet, dict)
        else ""
    )
    manifest_cards = manifest_html(inputs_used if isinstance(inputs_used, list) else [])
    input_status_cards = input_status_html(inputs_status if isinstance(inputs_status, list) else [])
    weekend_timeline = timeline_html(
        str(prediction.get("weekend_format") or race_config.get("weekend_format") or "standard"),
        list(available_sessions),
        target_session_code,
        race_config.get("sessions_schedule") if isinstance(race_config.get("sessions_schedule"), dict) else None,
        weather,
        live,
    )

    target_blurb = {
        "qualifying": "System is automatically estimating the next qualifying order from history, practice data and signals.",
        "sprint_qualifying": "System is automatically estimating sprint qualifying from history and the current sprint weekend setup.",
        "sprint": "System is automatically simulating the sprint using the sprint qualifying grid when available.",
        "race": "System is automatically simulating the race using the qualifying grid when available.",
    }.get(target, "System is automatically generating the current prediction target.")

    season_label = html.escape(str(prediction.get("season") or race_config.get("season") or ""))
    round_label = html.escape(str(race_config.get("next_round") or ""))
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="dark" />
    <title>{race_name} | APEX-F1</title>
    <style>{PAGE_CSS}</style>
  </head>
  <body>
    <main class="wrap">
      <header class="top theme-{target_theme}">
        <div class="top-row">
          <div>
            <p class="eyebrow">{season_label} &middot; Round {round_label} &middot; {weekend_format.title()} weekend</p>
            <h1>{race_name}</h1>
            <p class="meta">Predicting <b>{html.escape(target_label)}</b> &middot; sessions done: {html.escape(available_sessions_label)} &middot; <span data-updated="{generated_at}">updated {generated_at}</span></p>
          </div>
          <a class="pill-link" href="accuracy.html">How accurate is it? &rsaquo;</a>
        </div>
        {lock_html}
        <p class="why" title="Why This Is Active Now">{html.escape(why_now)}</p>
        {toggle_html}
      </header>

      <section class="card">
        <h2 class="card-title">Weekend Timeline</h2>
        {weekend_timeline}
      </section>

      <section class="card">
        <h2 class="card-title">Predictions</h2>
        {weather_banner}
        {dry_panel}
        {wet_panel}
      </section>

      {penalties_html(race_config)}

      {compounds_html}

      {track_record_html(track_record)}

      <section class="card">{HELP_HTML}</section>

      <section class="card">
        <details class="debug-panel">
          <summary>Technical Details</summary>
          <p class="muted">Why This Is Active Now: {html.escape(why_now)}</p>
          <div class="debug-grid">
            <article class="debug-card"><p class="debug-label">Season Blend</p><p class="debug-value">{season_blend_summary}</p></article>
            <article class="debug-card"><p class="debug-label">Target Session</p><p class="debug-value">{target_session_code}</p></article>
            <article class="debug-card"><p class="debug-label">Grid Source</p><p class="debug-value">{grid_source}</p></article>
            <article class="debug-card"><p class="debug-label">Article Signals</p><p class="debug-value">{signal_count or "none (weight moved to timing data)"}</p></article>
            <article class="debug-card"><p class="debug-label">Simulations</p><p class="debug-value">{simulations}</p></article>
            <article class="debug-card"><p class="debug-label">Output Type</p><p class="debug-value">{html.escape(target_output_type)}</p></article>
            <article class="debug-card"><p class="debug-label">Generated</p><p class="debug-value">{generated_at}</p></article>
          </div>
          <h3 class="subhead">Input Weights</h3>
          <section class="inputs-grid">{manifest_cards}</section>
          <h3 class="subhead">Input Availability</h3>
          <section class="input-status-grid">{input_status_cards}</section>
        </details>
      </section>
      <p class="footnote">APEX-F1 &middot; Monte Carlo simulation of the next F1 session &middot; data: FastF1, OpenF1, FIA, Open-Meteo</p>
    </main>
{script_html}
    <script>
      // "updated 12 min ago" in the visitor's clock.
      for (const el of document.querySelectorAll('[data-updated]')) {{
        const d = new Date(el.dataset.updated);
        if (isNaN(d)) continue;
        const mins = Math.round((Date.now() - d) / 60000);
        el.textContent = mins < 1 ? 'updated just now' : mins < 60 ? 'updated ' + mins + ' min ago'
          : mins < 1440 ? 'updated ' + Math.round(mins / 60) + ' h ago' : 'updated ' + d.toLocaleString();
        el.title = d.toLocaleString();
      }}
      // Expandable driver detail rows in the desktop table.
      for (const btn of document.querySelectorAll('.detail-toggle')) {{
        btn.addEventListener('click', () => {{
          const row = document.getElementById(btn.getAttribute('aria-controls'));
          if (!row) return;
          const open = btn.getAttribute('aria-expanded') === 'true';
          btn.setAttribute('aria-expanded', open ? 'false' : 'true');
          row.hidden = open;
        }});
      }}
      // Session times in the visitor's local time, and a countdown to the next one.
      for (const el of document.querySelectorAll('[data-local-time]')) {{
        const d = new Date(el.dataset.localTime);
        if (!isNaN(d)) el.textContent = d.toLocaleString([], {{ weekday: 'short', hour: '2-digit', minute: '2-digit' }});
      }}
      const countdownEl = document.querySelector('[data-countdown]');
      function tick() {{
        if (!countdownEl) return;
        const target = new Date(countdownEl.dataset.countdown);
        const value = countdownEl.querySelector('.countdown-value');
        const ms = target - new Date();
        if (isNaN(target) || !value) return;
        if (ms <= 0) {{ value.textContent = 'in progress or finished'; return; }}
        const d = Math.floor(ms / 86400000), h = Math.floor(ms / 3600000) % 24, m = Math.floor(ms / 60000) % 60;
        const when = target.toLocaleString([], {{ weekday: 'short', hour: '2-digit', minute: '2-digit' }});
        value.textContent = 'in ' + (d ? d + 'd ' : '') + h + 'h ' + m + 'm (' + when + ')';
      }}
      tick();
      setInterval(tick, 30000);
    </script>
  </body>
</html>
"""


PAGE_CSS = """
:root {
  color-scheme: dark;
  --bg: #0a0a0c; --surface: #151517; --surface-2: #1c1c1f; --line: rgba(255, 255, 255, 0.08);
  --ink: #f5f5f7; --muted: #a1a1a6; --faint: #6e6e73; --accent: #0a84ff; --accent-soft: rgba(10, 132, 255, 0.16);
  --up: #30d158; --down: #ff453a; --live: #ff453a; --track: #2a2a2e; --chip: #26262a; --shadow: none; --radius: 14px;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", Inter, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased; font-variant-numeric: tabular-nums;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 28px 16px 64px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px 20px; margin: 0 0 16px; }
.card-title, .section-title { margin: 0 0 12px; font-size: 0.78rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
h1 { margin: 2px 0 6px; font-size: clamp(1.7rem, 4vw, 2.4rem); font-weight: 700; letter-spacing: -0.02em; }
.eyebrow { margin: 0; color: var(--muted); font-size: 0.82rem; }
.top { margin-bottom: 18px; }
.top-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.meta { margin: 0; color: var(--muted); }
.meta b { color: var(--ink); font-weight: 600; }
.why { margin: 10px 0 0; color: var(--muted); font-size: 0.9rem; max-width: 72ch; }
.pill-link { display: inline-flex; align-items: center; gap: 4px; padding: 6px 12px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-weight: 600; font-size: 0.88rem; }
.pill-link:hover { text-decoration: none; filter: brightness(1.05); }
.live-banner { display: flex; align-items: center; gap: 8px; margin: 14px 0 0; padding: 10px 14px; border-radius: 12px; background: rgba(255, 59, 48, 0.08); font-size: 0.9rem; }
.live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--live); box-shadow: 0 0 0 4px rgba(255, 59, 48, 0.18); flex: none; }
.scenario-toggle { display: inline-flex; margin-top: 14px; padding: 3px; border-radius: 10px; background: var(--chip); gap: 2px; }
.toggle-btn { border: 0; background: transparent; color: var(--ink); font: inherit; font-size: 0.86rem; padding: 6px 14px; border-radius: 8px; cursor: pointer; }
.toggle-btn.is-active { background: var(--surface); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12); font-weight: 600; }
.next-session { margin: 0 0 12px; color: var(--muted); }
.next-session strong { color: var(--ink); }
.timeline-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.timeline-step { border-radius: 12px; padding: 12px; background: var(--surface-2); border: 1px solid transparent; }
.timeline-step p { margin: 0; font-weight: 600; }
.timeline-step time { display: block; color: var(--muted); font-size: 0.85rem; margin-top: 2px; }
.timeline-step > span { display: inline-block; margin-top: 6px; font-size: 0.72rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--faint); }
.timeline-current { border-color: var(--accent); background: var(--accent-soft); }
.timeline-current > span { color: var(--accent); }
.timeline-live { border-color: var(--live); background: rgba(255, 59, 48, 0.08); }
.timeline-live > span { color: var(--live); }
.timeline-done { opacity: 0.6; }
.wx { margin-top: 6px; }
.wx summary { cursor: pointer; list-style: none; display: inline-flex; align-items: center; gap: 6px; font-size: 0.82rem; color: var(--muted); }
.wx summary::-webkit-details-marker, .weather-banner summary::-webkit-details-marker, .page-help summary::-webkit-details-marker { display: none; }
.wx-icon { font-size: 1.3rem; line-height: 1; }
.wx.rain-high .wx-word { color: var(--accent); font-weight: 600; }
.timeline-step .wx-detail, .wx-detail { display: block; margin-top: 4px; font-size: 0.76rem; color: var(--muted); line-height: 1.4; text-transform: none; letter-spacing: normal; }
.weather-banner { margin: 0 0 14px; }
.weather-banner summary { cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px; font-weight: 500; }
.weather-banner p { margin: 8px 0 0; font-size: 0.85rem; color: var(--muted); }
.scenario-panel { display: none; }
.scenario-panel.is-active { display: block; }
.hero-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 14px; }
.hero-card { position: relative; border-radius: 12px; background: var(--surface-2); padding: 14px 16px 12px 18px; overflow: hidden; }
.hero-card::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 4px; background: var(--team-color); }
.hero-rank { margin: 0; font-size: 0.8rem; color: var(--muted); display: flex; gap: 8px; align-items: center; }
.hero-card h3 { margin: 4px 0 0; font-size: 1.3rem; font-weight: 700; display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.hero-team { margin: 0; color: var(--muted); font-size: 0.86rem; }
.hero-big { margin: 10px 0 8px; font-size: 2rem; font-weight: 700; letter-spacing: -0.02em; }
.hero-big small { margin-left: 4px; font-size: 0.82rem; font-weight: 500; color: var(--muted); }
.hero-metric { margin: 8px 0 0; color: var(--muted); font-size: 0.86rem; }
.odds-bar { position: relative; height: 6px; border-radius: 999px; background: var(--track); overflow: hidden; min-width: 120px; }
.odds-bar span { position: absolute; inset: 0 auto 0 0; border-radius: 999px; background: var(--team-color); }
.odds-l2 { opacity: 0.45; }
.odds-l3 { opacity: 0.2; }
.odds-legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 0 0 10px; color: var(--muted); font-size: 0.8rem; }
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.legend-item i { display: inline-block; width: 16px; height: 6px; border-radius: 999px; background: var(--ink); }
.legend-l2 { opacity: 0.45; }
.legend-l3 { opacity: 0.2; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: 0.72rem; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--faint); padding: 8px 10px; border-bottom: 1px solid var(--line); }
td { padding: 10px; border-bottom: 1px solid var(--line); vertical-align: middle; }
tbody tr:last-child td { border-bottom: 0; }
.desktop-table td:first-child { color: var(--muted); width: 36px; }
.desktop-table td small { display: block; color: var(--muted); font-size: 0.8rem; }
.odds-cell small { margin-top: 4px; font-variant-numeric: tabular-nums; }
.mobile-list { display: none; }
.detail-toggle { all: unset; cursor: pointer; display: inline-flex; align-items: center; gap: 4px; }
.detail-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.detail-toggle .caret { font-size: 0.7em; color: var(--faint); transition: transform 0.15s; }
.detail-toggle[aria-expanded="true"] .caret { transform: rotate(180deg); }
.detail-row td { background: var(--surface-2); }
.driver-detail-body { display: flex; flex-wrap: wrap; gap: 10px 28px; align-items: center; font-size: 0.86rem; }
.driver-detail-body .facts { display: flex; flex-direction: column; gap: 4px; }
.driver-detail-body .fact b { color: var(--muted); font-weight: 600; }
.trend-up { color: var(--up); font-weight: 700; }
.trend-down { color: var(--down); font-weight: 700; }
.session-chip { display: inline-block; margin-left: 4px; padding: 0 6px; border-radius: 6px; background: var(--chip); font-size: 0.8em; white-space: nowrap; }
.pos-hist-wrap { display: flex; flex-direction: column; gap: 2px; }
.hist-label { color: var(--muted); font-size: 0.75rem; }
.pos-hist { width: 230px; max-width: 100%; height: auto; }
.pos-hist-ticks text { fill: var(--faint); font-size: 8px; }
.driver-detail { margin-top: 6px; }
.driver-detail summary { cursor: pointer; color: var(--accent); font-size: 0.82rem; }
.driver-detail .driver-detail-body { margin-top: 8px; }
.delta { font-size: 0.82rem; font-weight: 600; white-space: nowrap; }
.tri { width: 0.7em; height: 0.7em; vertical-align: 0; margin-right: 1px; }
.delta-up { color: var(--up); }
.delta-down { color: var(--down); }
.delta-flat { color: var(--faint); }
.penalty-badge, .sub-badge { display: inline-block; padding: 1px 7px; border-radius: 6px; font-size: 0.7rem; font-weight: 600; white-space: nowrap; cursor: help; vertical-align: middle; }
.penalty-badge { background: rgba(255, 59, 48, 0.12); color: var(--down); }
.sub-badge { background: var(--accent-soft); color: var(--accent); }
.info-list { list-style: none; margin: 0; padding: 0; }
.info-list li { display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.info-list li:last-child { border-bottom: 0; }
.info-list .muted, .muted { color: var(--muted); }
.subhead { margin: 10px 0 2px; font-size: 0.86rem; font-weight: 600; }
.tyre-grid { display: flex; gap: 10px; flex-wrap: wrap; }
.tyre-item { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 10px; background: var(--surface-2); }
.tyre-item::before { content: ""; width: 12px; height: 12px; border-radius: 50%; border: 3px solid var(--tyre); }
.tyre-hard { --tyre: #d8d8dc; } .tyre-medium { --tyre: #f5c400; } .tyre-soft { --tyre: #e10600; }
.tyre-label, .tyre-value { margin: 0; }
.tyre-label { color: var(--muted); font-size: 0.82rem; }
.tyre-value { font-weight: 600; }
.page-help summary, .debug-panel summary { cursor: pointer; list-style: none; font-weight: 600; }
.page-help dl { margin: 12px 0 4px; display: grid; gap: 6px 18px; grid-template-columns: minmax(140px, 220px) 1fr; font-size: 0.88rem; }
.page-help dt { font-weight: 600; }
.page-help dd { margin: 0; color: var(--muted); line-height: 1.45; }
.debug-grid, .inputs-grid, .input-status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin: 12px 0; }
.debug-card, .input-card, .status-input-card, .empty-card { border-radius: 10px; background: var(--surface-2); padding: 10px 12px; }
.debug-label, .input-source { margin: 0; color: var(--muted); font-size: 0.78rem; }
.debug-value, .input-key { margin: 2px 0 0; font-weight: 600; font-size: 0.9rem; word-break: break-word; }
.input-bar { height: 4px; border-radius: 999px; background: var(--track); margin: 8px 0 4px; overflow: hidden; }
.input-bar span { display: block; height: 100%; background: var(--accent); }
.input-weight { margin: 0; color: var(--muted); font-size: 0.8rem; }
.status-badge { display: inline-block; margin-top: 6px; padding: 1px 7px; border-radius: 6px; background: var(--chip); font-size: 0.72rem; }
.table-scroll { overflow-x: auto; }
.rank { display: inline-block; min-width: 1.4em; color: var(--muted); font-weight: 600; }
.footnote { margin: 24px 0 0; color: var(--faint); font-size: 0.78rem; text-align: center; }
@media (max-width: 760px) {
  .hero-grid { display: none; }
  .desktop-table { display: none; }
  .mobile-list { display: grid; gap: 10px; }
  .mobile-driver-card { position: relative; border-radius: 12px; background: var(--surface-2); padding: 12px 14px 12px 16px; overflow: hidden; }
  .mobile-driver-card::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 4px; background: var(--team-color); }
  .mobile-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
  .mobile-top h4 { margin: 0; font-size: 1rem; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .mobile-top span { color: var(--muted); font-size: 0.82rem; }
  .mobile-driver-card p { margin: 8px 0 0; color: var(--muted); font-size: 0.84rem; }
  .page-help dl { grid-template-columns: 1fr; }
  .card { padding: 16px; }
  .timeline-grid { grid-template-columns: repeat(2, 1fr); }
}
"""


def locked_prediction(archive_dir: Path, race_config: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """The prediction archived before the target session, once that session
    has started (until its results are ingested and the target moves on)."""
    code = str(race_config.get("target_session_code") or "").upper()
    schedule = race_config.get("sessions_schedule") if isinstance(race_config.get("sessions_schedule"), dict) else {}
    start = parse_utc(schedule.get(code))
    if not code or start is None or now < start:
        return None
    try:
        path = archive_dir / str(int(race_config.get("season"))) / f"{int(race_config.get('next_round')):02d}_{code}.json"
    except (TypeError, ValueError):
        return None
    return load_optional_json(path)


def load_optional_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = load_json(path) if path.exists() else None
    except Exception as exc:
        LOGGER.warning("Ignoring unreadable %s: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def load_prediction_for_render(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any] | None]:
    dry_path = Path(args.prediction_dry)
    wet_path = Path(args.prediction_wet)
    if dry_path.exists() and wet_path.exists():
        dry = load_json(dry_path)
        wet = load_json(wet_path)
        if not isinstance(dry, dict) or not isinstance(wet, dict):
            raise ValueError("Dry/wet prediction input must be JSON objects.")
        return dry, wet

    single_path = Path(args.prediction)
    if single_path.exists():
        single = load_json(single_path)
        if not isinstance(single, dict):
            raise ValueError("Prediction input must be a JSON object.")
        return single, None

    raise FileNotFoundError("Missing prediction input.")


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        prediction, prediction_wet = load_prediction_for_render(args)
    except FileNotFoundError as exc:
        if args.allow_missing_input:
            LOGGER.warning("Skipping render step, prediction input missing: %s", exc)
            return 0
        LOGGER.error("render_prediction_page failed: %s", exc)
        return 1
    except Exception as exc:
        LOGGER.error("render_prediction_page failed: %s", exc)
        return 1

    try:
        race_config: dict[str, Any] = {}
        race_config_path = Path(args.race_config)
        if race_config_path.exists():
            raw = load_json(race_config_path)
            if isinstance(raw, dict):
                race_config = raw
        season = int(to_float(prediction.get("season"), to_float(race_config.get("season"), 0)))
        race_name = str(prediction.get("race") or race_config.get("race") or "")
        tyre_compounds = load_tyre_compounds(Path(args.tyres_input), season, race_name) if season > 0 and race_name else None
        track_record_path = Path(args.track_record)
        track_record = load_json(track_record_path) if track_record_path.exists() else None
        rendered = render_page(
            prediction,
            race_config,
            prediction_wet=prediction_wet,
            tyre_compounds=tyre_compounds,
            track_record=track_record if isinstance(track_record, dict) else None,
            weather=load_optional_json(Path(args.weather)),
            history=load_optional_json(Path(args.history)),
            locked=locked_prediction(Path(args.archive_dir), race_config, datetime.now(timezone.utc)),
            weekend_results=weekend_session_results(
                load_optional_json(Path(args.raw_dir) / f"season_{season}.json"), race_config.get("next_round")
            ),
        )
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    except Exception as exc:
        LOGGER.error("render_prediction_page failed: %s", exc)
        return 1

    LOGGER.info("Rendered prediction page: %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
