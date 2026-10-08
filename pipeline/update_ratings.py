#!/usr/bin/env python3
"""
Update deterministic model ratings from processed features and signals.

Inputs:
- data/processed/features_season_<year>.json
- knowledge/processed/*.json

Outputs:
- models/driver_ratings.json
- models/team_ratings.json
- models/strategy_scores.json
- models/reliability_scores.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("update_ratings")
FEATURE_FILE_RE = re.compile(r"^features_season_(\d{4})\.json$")
DEFAULT_SIGNAL_GUARDRAILS = {
    "source_credibility": {
        "the-race": 0.90,
        "racefans": 0.86,
        "motorsport": 0.86,
        "autosport": 0.85,
    },
    "default_source_credibility": 0.45,
    "source_confidence_floor": 0.2,
    "echo_decay": 0.6,
    "penalty_index_cap": 0.35,
}
SESSION_CHRONOLOGY = {
    "FP1": 1,
    "FP2": 2,
    "FP3": 3,
    "SQ": 4,
    "S": 5,
    "Q": 6,
    "R": 7,
}
COMPETITIVE_SESSION_CODES = {"SQ", "S", "Q", "R"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update deterministic rating JSON files.")
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Season year. If omitted, inferred from latest features file.",
    )
    parser.add_argument(
        "--features-input",
        default="data/processed",
        help="Features file path or directory containing features_season_*.json.",
    )
    parser.add_argument(
        "--signals-dir",
        default="knowledge/processed",
        help="Directory with processed signal JSON files.",
    )
    parser.add_argument(
        "--models-dir",
        default="models",
        help="Directory to write model JSON files.",
    )
    parser.add_argument(
        "--guardrails-config",
        default="config/signal_guardrails.json",
        help="Signal guardrails configuration JSON path.",
    )
    parser.add_argument(
        "--allow-missing-features",
        action="store_true",
        help="Exit 0 when features file is missing.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    text = str(value).strip()
    if not text:
        return None
    try:
        f = float(text)
    except ValueError:
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def slug(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


def stable_hash_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_signal_guardrails(path: Path) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_SIGNAL_GUARDRAILS))
    if not path.exists():
        return merged
    try:
        raw = load_json(path)
    except Exception as exc:
        LOGGER.warning("Could not parse signal guardrails config %s: %s", path, exc)
        return merged
    if not isinstance(raw, dict):
        return merged

    credibility = raw.get("source_credibility")
    if isinstance(credibility, dict):
        normalized: dict[str, float] = {}
        for key, value in credibility.items():
            if not isinstance(key, str):
                continue
            num = safe_float(value)
            if num is None:
                continue
            normalized[key.strip().lower()] = clamp(num, 0.0, 1.0)
        if normalized:
            merged["source_credibility"] = normalized

    for key in ("default_source_credibility", "source_confidence_floor", "echo_decay", "penalty_index_cap"):
        num = safe_float(raw.get(key))
        if num is not None:
            merged[key] = clamp(num, 0.0, 1.0)

    return merged


def season_from_features_path(path: Path) -> int | None:
    match = FEATURE_FILE_RE.match(path.name)
    if not match:
        return None
    return int(match.group(1))


def features_has_driver_rows(path: Path) -> bool:
    try:
        payload = load_json(path)
    except Exception:
        return False
    rows = payload.get("drivers")
    return isinstance(rows, list) and len(rows) > 0


def choose_features_file(features_input: Path, season: int | None) -> Path:
    if features_input.is_file():
        return features_input

    if not features_input.exists():
        raise FileNotFoundError(f"Features path does not exist: {features_input}")

    candidates = sorted(features_input.glob("features_season_*.json"))
    if not candidates:
        raise FileNotFoundError(f"No features files in {features_input}")

    if season is not None:
        candidate = features_input / f"features_season_{season}.json"
        if candidate.exists() and features_has_driver_rows(candidate):
            return candidate

        fallback_candidates = sorted(
            (p for p in candidates if (season_from_features_path(p) or 0) < season),
            key=lambda p: season_from_features_path(p) or -1,
            reverse=True,
        )
        for path in fallback_candidates:
            if features_has_driver_rows(path):
                LOGGER.warning(
                    "Requested season %s features have no driver rows; falling back to season %s features %s",
                    season,
                    season_from_features_path(path),
                    path,
                )
                return path
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"Features file not found for season {season} and no fallback with driver rows.")

    by_season_desc = sorted(
        candidates,
        key=lambda p: season_from_features_path(p) or -1,
        reverse=True,
    )
    for path in by_season_desc:
        if features_has_driver_rows(path):
            return path
    return by_season_desc[0]


def normalize_signals(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        signals = raw.get("signals")
        if isinstance(signals, list):
            return [x for x in signals if isinstance(x, dict)]
        return [raw]
    return []


def load_signals(signals_dir: Path) -> list[dict[str, Any]]:
    if not signals_dir.exists():
        return []
    signals: list[dict[str, Any]] = []
    for file_path in sorted(signals_dir.glob("*.json")):
        try:
            raw = load_json(file_path)
        except json.JSONDecodeError as exc:
            LOGGER.warning("Skipping invalid signal JSON %s: %s", file_path, exc)
            continue
        signals.extend(normalize_signals(raw))
    return signals


def signal_weight(signal: dict[str, Any], guardrails: dict[str, Any]) -> float:
    source_name = str(signal.get("source_name") or "").strip().lower()
    source_credibility = guardrails.get("source_credibility", {})
    if not isinstance(source_credibility, dict):
        source_credibility = {}
    credibility = safe_float(source_credibility.get(source_name))
    if credibility is None:
        credibility = safe_float(guardrails.get("default_source_credibility"))
    if credibility is None:
        credibility = 0.45
    credibility = clamp(credibility, 0.0, 1.0)

    source_confidence = safe_float(signal.get("source_confidence"))
    if source_confidence is None:
        source_confidence = 0.5
    source_confidence = clamp(source_confidence, 0.0, 1.0)
    floor = safe_float(guardrails.get("source_confidence_floor"))
    if floor is None:
        floor = 0.2
    if source_confidence < clamp(floor, 0.0, 1.0):
        return 0.0
    return source_confidence * credibility


def aggregate_optional_signal_indexes(signals: list[dict[str, Any]], guardrails: dict[str, Any]) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    # Team wet index, team safety-car index, team penalty index.
    wet_sum: dict[str, float] = {}
    wet_w: dict[str, float] = {}
    safety_sum: dict[str, float] = {}
    safety_w: dict[str, float] = {}
    penalty_sum: dict[str, float] = {}
    penalty_w: dict[str, float] = {}
    echo_decay = safe_float(guardrails.get("echo_decay"))
    if echo_decay is None:
        echo_decay = 0.6
    echo_decay = clamp(echo_decay, 0.0, 1.0)
    echo_counts: dict[str, int] = {}

    for signal in signals:
        team_key = slug(str(signal.get("team") or ""))
        if not team_key:
            continue
        base_weight = signal_weight(signal, guardrails=guardrails)
        if base_weight <= 0:
            continue

        wet = safe_float(signal.get("wet_performance_index"))
        if wet is not None:
            wet = clamp(wet, 0.0, 1.0)
            fp = f"wet|{team_key}|{round(wet, 2)}"
            seen = echo_counts.get(fp, 0)
            weight = base_weight * (echo_decay**seen)
            echo_counts[fp] = seen + 1
            if weight > 0:
                wet_sum[team_key] = wet_sum.get(team_key, 0.0) + weight * wet
                wet_w[team_key] = wet_w.get(team_key, 0.0) + weight

        safety = safe_float(signal.get("safety_car_reaction"))
        if safety is not None:
            safety = clamp(safety, 0.0, 1.0)
            fp = f"safety|{team_key}|{round(safety, 2)}"
            seen = echo_counts.get(fp, 0)
            weight = base_weight * (echo_decay**seen)
            echo_counts[fp] = seen + 1
            if weight > 0:
                safety_sum[team_key] = safety_sum.get(team_key, 0.0) + weight * safety
                safety_w[team_key] = safety_w.get(team_key, 0.0) + weight

        penalty = safe_float(signal.get("new_component_penalty"))
        if penalty is not None:
            penalty = clamp(penalty, 0.0, 1.0)
            fp = f"penalty|{team_key}|{round(penalty, 2)}"
            seen = echo_counts.get(fp, 0)
            weight = base_weight * (echo_decay**seen)
            echo_counts[fp] = seen + 1
            if weight > 0:
                penalty_sum[team_key] = penalty_sum.get(team_key, 0.0) + weight * penalty
                penalty_w[team_key] = penalty_w.get(team_key, 0.0) + weight

    def avg_map(sum_map: dict[str, float], weight_map: dict[str, float], cap: float | None = None) -> dict[str, float]:
        out: dict[str, float] = {}
        for key in sorted(sum_map.keys()):
            w = weight_map.get(key, 0.0)
            if w <= 0:
                continue
            value = sum_map[key] / w
            if cap is not None:
                value = min(value, cap)
            out[key] = round(value, 6)
        return out

    penalty_cap = safe_float(guardrails.get("penalty_index_cap"))
    if penalty_cap is None:
        penalty_cap = 0.35
    penalty_cap = clamp(penalty_cap, 0.0, 1.0)

    return (
        avg_map(wet_sum, wet_w),
        avg_map(safety_sum, safety_w),
        avg_map(penalty_sum, penalty_w, cap=penalty_cap),
    )


def load_current_entry_list(raw_dir: Path, season: int) -> tuple[dict[str, str], list[str]]:
    # Returns (driver_to_team_map, list_of_active_teams)
    path = raw_dir / f"season_{season}.json"
    if not path.exists():
        return {}, []
    try:
        payload = load_json(path)
    except Exception:
        return {}, []

    latest_competitive: dict[str, str] = {}
    latest_any: dict[str, str] = {}

    events = payload.get("events", [])
    if not isinstance(events, list):
        return {}, []

    def event_key(indexed_event: tuple[int, dict[str, Any]]) -> tuple[str, float, int]:
        index, event = indexed_event
        raw_date = str(event.get("event_date") or "").strip()
        event_date = raw_date[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", raw_date) else "9999-12-31"
        round_number = safe_float(event.get("round"))
        return (event_date, round_number if round_number is not None else 9999.0, index)

    for _, event in sorted(
        ((idx, event) for idx, event in enumerate(events) if isinstance(event, dict)),
        key=event_key,
    ):
        sessions = event.get("sessions", [])
        if not isinstance(sessions, list):
            continue
        ordered_sessions = sorted(
            (session for session in sessions if isinstance(session, dict)),
            key=lambda session: SESSION_CHRONOLOGY.get(str(session.get("session_code") or "").upper(), 99),
        )
        for session in ordered_sessions:
            code = str(session.get("session_code") or "").upper()
            roster: dict[str, str] = {}
            results = session.get("results", [])
            if not isinstance(results, list):
                continue
            for res in results:
                if not isinstance(res, dict):
                    continue
                driver = str(res.get("abbreviation") or "").strip().upper()
                team = str(res.get("team_name") or "").strip()
                if driver and team:
                    roster[driver] = team
            if not roster:
                continue
            latest_any = roster
            if code in COMPETITIVE_SESSION_CODES:
                latest_competitive = roster

    mapping = latest_competitive or latest_any
    teams = sorted(set(mapping.values()))
    return mapping, teams


def component(value: float) -> float:
    """Every rating component lives on a 0-100 scale."""
    return round(clamp(value, 0.0, 100.0), 6)


def weighted_component_mean(parts: list[tuple[float | None, float]], fallback: float = 50.0) -> float:
    """Weighted mean over the components that have a real input. Missing
    inputs are dropped and the remaining weights renormalised, instead of
    being replaced by a constant that is identical for every driver/team."""
    total = 0.0
    weight_sum = 0.0
    for value, weight in parts:
        if value is None or weight <= 0:
            continue
        total += weight * value
        weight_sum += weight
    if weight_sum <= 0:
        return fallback
    return total / weight_sum


def percentile(values: list[float], q: float) -> float | None:
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    pos = clamp(q, 0.0, 1.0) * (len(data) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least squares y = a + b*x. None when x has no spread."""
    if len(points) < 2:
        return None
    mean_x = statistics.fmean(x for x, _ in points)
    mean_y = statistics.fmean(y for _, y in points)
    var_x = sum((x - mean_x) ** 2 for x, _ in points)
    if var_x <= 1e-9:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / var_x
    return mean_y - slope * mean_x, slope


def race_position_gain_by_team(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Places gained from qualifying to the race, relative to what the field
    trend predicts for that qualifying position. Raw (q_avg - race_avg)
    punishes front-runners, who cannot gain places, so the regression
    residual is used instead. Positive = better than expected in races."""
    points: dict[str, tuple[float, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        q_avg = safe_float(row.get("qualifying_avg_position"))
        race_avg = safe_float(row.get("race_avg_position"))
        team = str(row.get("team") or "").strip()
        if team and q_avg is not None and race_avg is not None:
            points[team] = (q_avg, race_avg)
    fit = linear_fit(list(points.values()))
    out: dict[str, float] = {}
    for team, (q_avg, race_avg) in points.items():
        expected = fit[0] + fit[1] * q_avg if fit else q_avg
        out[team] = round(expected - race_avg, 6)
    return out


def wet_rating(row: dict[str, Any], team_wet_index: float | None) -> float:
    """Wet-weather skill, 50 = same as in the dry. Built from places gained
    in wet sessions (shrunk by how few there were) and, when present, the
    team's wet-performance signal."""
    delta = safe_float(row.get("wet_position_delta"))
    sessions = safe_float(row.get("wet_sessions")) or 0.0
    history = None
    if delta is not None and sessions > 0:
        history = component(50.0 + 8.0 * clamp(delta, -4.0, 4.0) * sessions / (sessions + 2.0))
    signal = component(35.0 + 30.0 * clamp(team_wet_index, 0.0, 1.0)) if team_wet_index is not None else None
    return round(weighted_component_mean([(history, 1.0), (signal, 0.5)], fallback=50.0), 6)


def compute_driver_ratings(features: dict[str, Any], wet_by_team: dict[str, float], active_drivers: dict[str, str]) -> dict[str, Any]:
    rows = features.get("drivers", [])
    if not isinstance(rows, list):
        rows = []

    feature_map = {str(row.get("driver") or "").strip().upper(): row for row in rows if isinstance(row, dict)}

    # Teammate deltas based on average race position.
    by_team: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        team_key = slug(str(row.get("team") or ""))
        driver_name = str(row.get("driver") or "").strip().upper()
        race_avg = safe_float(row.get("race_avg_position"))
        if not team_key or not driver_name or race_avg is None:
            continue
        by_team.setdefault(team_key, []).append((driver_name, race_avg))

    teammate_delta: dict[str, float] = {}
    for team_key, pairs in by_team.items():
        if len(pairs) < 2:
            continue
        team_mean = statistics.fmean(v for _, v in pairs)
        for driver_name, race_avg in pairs:
            teammate_delta[driver_name] = round(team_mean - race_avg, 6)

    rated: dict[str, dict[str, Any]] = {}
    rookies: list[tuple[str, str]] = []
    for driver_name, team_name in sorted(active_drivers.items()):
        row = feature_map.get(driver_name)
        if row is None:
            rookies.append((driver_name, team_name))
            continue

        race_avg = safe_float(row.get("race_avg_position"))
        race_gap = safe_float(row.get("race_gap_to_winner_seconds"))
        race_lap_gap = safe_float(row.get("race_lap_pace_gap_seconds"))
        q_avg = safe_float(row.get("qualifying_avg_position"))
        q_gap_ms = safe_float(row.get("qualifying_gap_to_best_ms"))
        teammate_q_gap_ms = safe_float(row.get("teammate_qualifying_gap_ms"))
        practice_avg = safe_float(row.get("practice_avg_position"))
        practice_lap_gap = safe_float(row.get("practice_lap_pace_gap_seconds"))
        sprint_q_avg = safe_float(row.get("sprint_qualifying_avg_position"))
        sprint_q_gap_ms = safe_float(row.get("sprint_qualifying_gap_to_best_ms"))
        sprint_lap_gap = safe_float(row.get("sprint_lap_pace_gap_seconds"))
        q_phase = safe_float(row.get("qualifying_phase_depth"))
        dnf_rate = safe_float(row.get("dnf_rate"))
        starts = safe_float(row.get("starts")) or 0.0
        recent_race_form = safe_float(row.get("race_form_last3"))
        signal_conf = safe_float(row.get("signal_driver_confidence_delta")) or 0.0

        sample_scale = clamp(starts / 5.0, 0.0, 1.0)
        t_delta = teammate_delta.get(driver_name)
        teammate_component = component(50.0 + 14.0 * clamp(t_delta, -2.0, 2.0) * sample_scale) if t_delta is not None else None
        consistency_component = component(75.0 - 40.0 * clamp(dnf_rate, 0.0, 1.0)) if dnf_rate is not None else None
        base_quali_component = component(80.0 - 2.8 * clamp(q_avg, 1.0, 20.0)) if q_avg is not None else None
        q_gap_component = component(86.0 - 0.030 * clamp(q_gap_ms, 0.0, 2400.0)) if q_gap_ms is not None else None
        # Gap to the faster teammate (>= 0): the faster driver scores 60.
        teammate_q_gap_component = (
            component(60.0 - 0.05 * clamp(teammate_q_gap_ms, 0.0, 800.0)) if teammate_q_gap_ms is not None else None
        )
        form_reference = recent_race_form if recent_race_form is not None else race_avg
        race_form_component = component(82.0 - 2.6 * clamp(form_reference, 1.0, 20.0)) if form_reference is not None else None
        race_pace_reference = race_lap_gap if race_lap_gap is not None else race_gap
        race_gap_component = component(84.0 - 0.65 * clamp(race_pace_reference, 0.0, 90.0)) if race_pace_reference is not None else None
        practice_component = None
        if practice_avg is not None or practice_lap_gap is not None:
            practice_component = component(
                weighted_component_mean(
                    [
                        (78.0 - 2.4 * clamp(practice_avg, 1.0, 20.0) if practice_avg is not None else None, 0.45),
                        (82.0 - 4.0 * clamp(practice_lap_gap, 0.0, 12.0) if practice_lap_gap is not None else None, 0.55),
                    ]
                )
            )
        sprint_quali_component = component(78.0 - 2.6 * clamp(sprint_q_avg, 1.0, 20.0)) if sprint_q_avg is not None else None
        sprint_q_gap_component = component(84.0 - 0.030 * clamp(sprint_q_gap_ms, 0.0, 2400.0)) if sprint_q_gap_ms is not None else None
        sprint_pace_component = component(82.0 - 4.0 * clamp(sprint_lap_gap, 0.0, 12.0)) if sprint_lap_gap is not None else None
        progression_component = component(45.0 + 18.0 * clamp(q_phase, 0.0, 1.0)) if q_phase is not None else None

        qualifying_component = weighted_component_mean(
            [
                (base_quali_component, 0.25),
                (q_gap_component, 0.30),
                (teammate_q_gap_component, 0.15),
                (practice_component, 0.15),
                (sprint_quali_component, 0.08),
                (sprint_q_gap_component, 0.04),
                (progression_component, 0.03),
            ]
        )
        race_component = weighted_component_mean(
            [
                (race_form_component, 0.27),
                (race_gap_component, 0.27),
                (consistency_component, 0.18),
                (teammate_component, 0.14),
                (qualifying_component, 0.10),
                (sprint_pace_component, 0.04),
            ]
        )

        signal_component = 5.0 * clamp(signal_conf, -1.0, 1.0)
        qualifying_rating = round(clamp(qualifying_component + signal_component, 0.0, 100.0), 6)
        race_rating = round(clamp(race_component + signal_component, 0.0, 100.0), 6)
        components = {
            "teammate_delta_performance": teammate_component,
            "consistency": consistency_component,
            "qualifying_pace": round(qualifying_component, 6),
            "qualifying_gap_pace": q_gap_component,
            "teammate_qualifying_gap": teammate_q_gap_component,
            "recent_race_form": race_form_component,
            "race_gap_pace": race_gap_component,
            "sprint_lap_pace": sprint_pace_component,
            "weekend_practice_pace": practice_component,
            "qualifying_progression": progression_component,
        }
        rated[driver_name] = {
            "driver": driver_name,
            "team": team_name,
            "qualifying_rating": qualifying_rating,
            "race_rating": race_rating,
            "wet_rating": wet_rating(row, wet_by_team.get(slug(team_name))),
            "components": {key: value for key, value in components.items() if value is not None},
        }

    # New drivers (e.g. a reserve driver called up): teammate level shifted
    # by how far the field's 25th percentile sits below its median. Without
    # a rated teammate, the field's 25th percentile itself.
    for driver_name, team_name in rookies:
        signal_conf = 0.0
        row = feature_map.get(driver_name) or {}
        signal_conf = safe_float(row.get("signal_driver_confidence_delta")) or 0.0
        values: dict[str, float] = {}
        for key in ("qualifying_rating", "race_rating"):
            field = [entry[key] for entry in rated.values()]
            p25 = percentile(field, 0.25)
            median = percentile(field, 0.5)
            mates = [entry[key] for entry in rated.values() if entry["team"] == team_name]
            if p25 is None or median is None:
                base = 50.0
            elif mates:
                base = statistics.fmean(mates) + (p25 - median)
            else:
                base = p25
            values[key] = round(clamp(base + 5.0 * clamp(signal_conf, -1.0, 1.0), 0.0, 100.0), 6)
        rated[driver_name] = {
            "driver": driver_name,
            "team": team_name,
            "qualifying_rating": values["qualifying_rating"],
            "race_rating": values["race_rating"],
            "wet_rating": wet_rating({}, wet_by_team.get(slug(team_name))),
            "components": {"new_driver_baseline": True},
        }

    payload_rows: list[dict[str, Any]] = []
    for driver_name in sorted(rated):
        entry = rated[driver_name]
        entry["driver_rating"] = round(clamp(0.45 * entry["qualifying_rating"] + 0.55 * entry["race_rating"], 0.0, 100.0), 6)
        payload_rows.append(entry)
    return {"drivers": payload_rows}


def compute_team_ratings(features: dict[str, Any], active_teams: list[str]) -> dict[str, Any]:
    rows = features.get("teams", [])
    if not isinstance(rows, list):
        rows = []

    feature_map = {str(row.get("team") or "").strip(): row for row in rows if isinstance(row, dict)}
    position_gain = race_position_gain_by_team(rows)

    q_values = [
        safe_float(row.get("qualifying_avg_position"))
        for row in rows
        if isinstance(row, dict) and safe_float(row.get("qualifying_avg_position")) is not None
    ]
    field_q_mean = statistics.fmean(q_values) if q_values else 10.5

    rated: dict[str, dict[str, Any]] = {}
    new_teams: list[str] = []
    for team_name in sorted(active_teams):
        row = feature_map.get(team_name)
        if row is None:
            new_teams.append(team_name)
            continue

        q_avg = safe_float(row.get("qualifying_avg_position"))
        q_gap_ms = safe_float(row.get("qualifying_gap_to_best_ms"))
        practice_avg = safe_float(row.get("practice_avg_position"))
        practice_lap_gap = safe_float(row.get("practice_lap_pace_gap_seconds"))
        sprint_q_avg = safe_float(row.get("sprint_qualifying_avg_position"))
        sprint_q_gap_ms = safe_float(row.get("sprint_qualifying_gap_to_best_ms"))
        q_phase = safe_float(row.get("qualifying_phase_depth"))
        race_avg = safe_float(row.get("race_avg_position"))
        race_gap = safe_float(row.get("race_gap_to_winner_seconds"))
        race_lap_gap = safe_float(row.get("race_lap_pace_gap_seconds"))
        sprint_lap_gap = safe_float(row.get("sprint_lap_pace_gap_seconds"))
        upgrade_score = safe_float(row.get("signal_upgrade_score"))

        q_inputs = [value for value in (q_avg, sprint_q_avg, practice_avg) if value is not None]
        q_gap_proxy = component(50.0 + 7.0 * clamp(field_q_mean - statistics.fmean(q_inputs), -6.0, 6.0)) if q_inputs else None
        quali_time_proxy = component(86.0 - 0.026 * clamp(q_gap_ms, 0.0, 2600.0)) if q_gap_ms is not None else None
        sprint_quali_time_proxy = component(84.0 - 0.026 * clamp(sprint_q_gap_ms, 0.0, 2600.0)) if sprint_q_gap_ms is not None else None
        race_pace_reference = race_lap_gap if race_lap_gap is not None else race_gap
        race_pace_proxy = component(84.0 - 0.58 * clamp(race_pace_reference, 0.0, 90.0)) if race_pace_reference is not None else None
        sprint_pace_proxy = component(82.0 - 4.0 * clamp(sprint_lap_gap, 0.0, 12.0)) if sprint_lap_gap is not None else None
        race_position_proxy = component(82.0 - 2.4 * clamp(race_avg, 1.0, 20.0)) if race_avg is not None else None
        gain = position_gain.get(team_name)
        race_position_gain = component(50.0 + 6.0 * clamp(gain, -5.0, 5.0)) if gain is not None else None
        upgrades_impact = component(45.0 + 16.0 * clamp(upgrade_score, 0.0, 3.0)) if upgrade_score is not None else None
        weekend_pace_proxy = None
        if practice_avg is not None or practice_lap_gap is not None:
            weekend_pace_proxy = component(
                weighted_component_mean(
                    [
                        (45.0 + 20.0 * clamp(1.0 - practice_avg / 20.0, 0.0, 1.0) if practice_avg is not None else None, 0.45),
                        (82.0 - 4.0 * clamp(practice_lap_gap, 0.0, 12.0) if practice_lap_gap is not None else None, 0.55),
                    ]
                )
            )
        progression_proxy = component(40.0 + 20.0 * clamp(q_phase, 0.0, 1.0)) if q_phase is not None else None

        qualifying_rating = weighted_component_mean(
            [
                (q_gap_proxy, 0.28),
                (quali_time_proxy, 0.34),
                (sprint_quali_time_proxy, 0.10),
                (upgrades_impact, 0.12),
                (weekend_pace_proxy, 0.08),
                (progression_proxy, 0.08),
            ]
        )
        race_rating = weighted_component_mean(
            [
                (race_pace_proxy, 0.34),
                (race_position_proxy, 0.21),
                (race_position_gain, 0.14),
                (sprint_pace_proxy, 0.03),
                (upgrades_impact, 0.14),
                (weekend_pace_proxy, 0.08),
                (qualifying_rating, 0.06),
            ]
        )
        components = {
            "qualifying_gap_proxy": q_gap_proxy,
            "qualifying_time_gap_proxy": quali_time_proxy,
            "sprint_qualifying_time_gap_proxy": sprint_quali_time_proxy,
            "race_position_gain": race_position_gain,
            "race_pace_proxy": race_pace_proxy,
            "race_position_proxy": race_position_proxy,
            "sprint_pace_proxy": sprint_pace_proxy,
            "upgrades_impact": upgrades_impact,
            "weekend_pace_proxy": weekend_pace_proxy,
            "qualifying_progression": progression_proxy,
        }
        rated[team_name] = {
            "team": team_name,
            "qualifying_team_rating": round(clamp(qualifying_rating, 0.0, 100.0), 6),
            "race_team_rating": round(clamp(race_rating, 0.0, 100.0), 6),
            "components": {key: value for key, value in components.items() if value is not None},
        }

    # A team without any data (new entrant) starts at the field's 25th
    # percentile rather than above the median.
    for team_name in new_teams:
        values = {}
        for key in ("qualifying_team_rating", "race_team_rating"):
            p25 = percentile([entry[key] for entry in rated.values()], 0.25)
            values[key] = round(p25 if p25 is not None else 50.0, 6)
        rated[team_name] = {"team": team_name, **values, "components": {"new_team_baseline": True}}

    payload_rows: list[dict[str, Any]] = []
    for team_name in sorted(rated):
        entry = rated[team_name]
        entry["team_rating"] = round(clamp(0.45 * entry["qualifying_team_rating"] + 0.55 * entry["race_team_rating"], 0.0, 100.0), 6)
        payload_rows.append(entry)
    return {"teams": payload_rows}


def compute_strategy_scores(features: dict[str, Any], safety_by_team: dict[str, float], active_teams: list[str]) -> dict[str, Any]:
    rows = features.get("teams", [])
    if not isinstance(rows, list):
        rows = []

    feature_map = {str(row.get("team") or "").strip(): row for row in rows if isinstance(row, dict)}
    position_gain = race_position_gain_by_team(rows)

    payload_rows: list[dict[str, Any]] = []
    for team_name in sorted(active_teams):
        team_key = slug(team_name)
        row = feature_map.get(team_name, {})

        sprint_q_avg = safe_float(row.get("sprint_qualifying_avg_position"))
        sprint_avg = safe_float(row.get("sprint_avg_position"))
        gain = position_gain.get(team_name)
        safety = safety_by_team.get(team_key)

        race_position_gain = component(50.0 + 8.0 * clamp(gain, -5.0, 5.0)) if gain is not None else None
        safety_reaction = component(40.0 + 40.0 * clamp(safety, 0.0, 1.0)) if safety is not None else None
        # Only real sprint qualifying vs sprint data says anything about
        # sprint execution; otherwise stay neutral.
        if sprint_q_avg is not None and sprint_avg is not None:
            sprint_execution = component(50.0 + 9.0 * clamp(sprint_q_avg - sprint_avg, -5.0, 5.0))
        else:
            sprint_execution = 50.0

        score = weighted_component_mean(
            [
                (race_position_gain, 0.60),
                (safety_reaction, 0.20),
                (sprint_execution, 0.20),
            ]
        )
        components = {
            "race_position_gain": race_position_gain,
            "safety_car_reactions": safety_reaction,
            "sprint_execution": sprint_execution,
        }
        payload_rows.append(
            {
                "team": team_name,
                "strategy_score": round(clamp(score, 0.0, 100.0), 6),
                "components": {key: value for key, value in components.items() if value is not None},
            }
        )

    return {"teams": payload_rows}


# Prior strength (in race starts) used to shrink a team's observed DNF rate
# towards the field average.
DNF_SHRINKAGE_STARTS = 8.0
DNF_PROBABILITY_BOUNDS = (0.01, 0.45)


def compute_reliability_scores(features: dict[str, Any], penalties_by_team: dict[str, float], active_teams: list[str]) -> dict[str, Any]:
    rows = features.get("teams", [])
    if not isinstance(rows, list):
        rows = []

    feature_map = {str(row.get("team") or "").strip(): row for row in rows if isinstance(row, dict)}

    total_starts = 0.0
    total_dnfs = 0.0
    for row in feature_map.values():
        starts = safe_float(row.get("starts")) or 0.0
        dnf_rate = safe_float(row.get("dnf_rate"))
        if starts > 0 and dnf_rate is not None:
            total_starts += starts
            total_dnfs += dnf_rate * starts
    field_rate = total_dnfs / total_starts if total_starts > 0 else 0.1

    payload_rows: list[dict[str, Any]] = []
    for team_name in sorted(active_teams):
        team_key = slug(team_name)
        row = feature_map.get(team_name, {})

        starts = safe_float(row.get("starts")) or 0.0
        dnf_rate = safe_float(row.get("dnf_rate"))
        dnfs = (dnf_rate or 0.0) * starts
        shrunk = (dnfs + DNF_SHRINKAGE_STARTS * field_rate) / (starts + DNF_SHRINKAGE_STARTS)

        signal_rel = safe_float(row.get("signal_reliability_concern"))
        penalty_idx = penalties_by_team.get(team_key)
        adjustment = 0.0
        if signal_rel is not None:
            adjustment += 0.08 * clamp(signal_rel, 0.0, 1.0)
        if penalty_idx is not None:
            adjustment += 0.04 * clamp(penalty_idx, 0.0, 1.0)

        dnf_probability = clamp(shrunk + adjustment, *DNF_PROBABILITY_BOUNDS)
        components = {
            "observed_dnf_rate": round(dnf_rate, 6) if dnf_rate is not None else None,
            "field_dnf_rate": round(field_rate, 6),
            "starts": starts,
            "signal_adjustment": round(adjustment, 6),
        }
        payload_rows.append(
            {
                "team": team_name,
                "dnf_probability": round(dnf_probability, 6),
                "reliability_score": round(100.0 * (1.0 - dnf_probability), 6),
                "components": {key: value for key, value in components.items() if value is not None},
            }
        )

    return {"teams": payload_rows}


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def blend_features(current: dict[str, Any], previous: dict[str, Any], current_weight: float) -> dict[str, Any]:
    # Blends driver and team features between two seasons based on weight (0.0 to 1.0)
    prev_weight = 1.0 - current_weight
    
    def blend_list(curr_list: list[dict[str, Any]], prev_list: list[dict[str, Any]], key_field: str) -> list[dict[str, Any]]:
        curr_map = {row[key_field]: row for row in curr_list if isinstance(row, dict) and key_field in row}
        prev_map = {row[key_field]: row for row in prev_list if isinstance(row, dict) and key_field in row}
        
        all_keys = set(curr_map.keys()) | set(prev_map.keys())
        blended: list[dict[str, Any]] = []
        
        # Numeric fields to blend
        numeric_fields = [
            "race_avg_position", "qualifying_avg_position", "dnf_rate", 
            "points_per_start", "points_total", "starts",
            "race_gap_to_winner_seconds", "race_lap_pace_gap_seconds",
            "qualifying_gap_to_best_ms", "teammate_qualifying_gap_ms",
            "practice_avg_position", "practice_lap_pace_gap_seconds",
            "fp1_avg_position", "fp2_avg_position", "fp3_avg_position",
            "sprint_qualifying_avg_position", "sprint_qualifying_gap_to_best_ms",
            "sprint_avg_position", "sprint_lap_pace_gap_seconds",
            "qualifying_phase_depth", "sprint_qualifying_phase_depth",
        ]
        
        for k in all_keys:
            c = curr_map.get(k, {})
            p = prev_map.get(k, {})
            
            # Start with current data as base (handles team names, etc.)
            row = dict(c) if c else dict(p)
            
            for field in numeric_fields:
                cv = safe_float(c.get(field))
                pv = safe_float(p.get(field))
                
                if cv is not None and pv is not None:
                    row[field] = (cv * current_weight) + (pv * prev_weight)
                elif cv is not None:
                    row[field] = cv
                elif pv is not None:
                    row[field] = pv
            
            blended.append(row)
        return blended

    return {
        "season": current.get("season"),
        "drivers": blend_list(current.get("drivers", []), previous.get("drivers", []), "driver"),
        "teams": blend_list(current.get("teams", []), previous.get("teams", []), "team")
    }


def current_season_blend_weight(max_starts: float) -> float:
    """Map effective sample size of current-season races to a [0,1] blend weight.

    Accepts either an integer count of completed races, or a fractional
    effective sample size (Kish ESS) derived from recency-weighted features.
    The schedule is intentionally aggressive: even one finished race already
    leans the model 45% toward the current season so mid-season car
    development, upgrades and team-order changes show up early.
    """
    starts = max(0.0, float(max_starts))
    if starts <= 0:
        return 0.0
    if starts >= 5:
        return 1.0

    # Pull the model toward current-season data faster so early form,
    # upgrades and team-order changes show up sooner in ratings.
    thresholds = (
        (1.0, 0.45),
        (2.0, 0.60),
        (3.0, 0.75),
        (4.0, 0.90),
    )
    for cutoff, weight in thresholds:
        if starts <= cutoff:
            return weight
    return 1.0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        features_path = choose_features_file(Path(args.features_input), args.season)
    except FileNotFoundError as exc:
        if args.allow_missing_features:
            LOGGER.warning("Skipping ratings update: %s", exc)
            return 0
        LOGGER.error("update_ratings failed: %s", exc)
        return 1

    try:
        features = load_json(features_path)
        season = int(features.get("season"))
        target_season = args.season if args.season is not None else season

        # SYSTEMIC FIX: Early season blending
        source_summary = f"Season {season} data"
        # 1. Determine the effective sample size of the current season.
        #    Prefer race_effective_starts (recency-weighted ESS produced by
        #    build_features) so a stale 5-race season counts less than a
        #    fresh 5-race season. Fall back to integer starts when ESS
        #    is missing for backward compatibility with old features files.
        max_starts = 0.0
        for dr in features.get("drivers", []):
            ess = safe_float(dr.get("race_effective_starts"))
            if ess is None:
                ess = safe_float(dr.get("starts")) or 0.0
            if ess > max_starts:
                max_starts = ess

        # 2. If it's early (e.g. < 5 races), try to load previous season for blending
        if 0 < max_starts < 5 and target_season == season:
            prev_season = season - 1
            prev_path = Path(args.features_input) / f"features_season_{prev_season}.json"
            if prev_path.exists():
                previous_features = load_json(prev_path)
                weight = current_season_blend_weight(max_starts)
                LOGGER.info("Blending features: season %s (weight %.1f) + season %s (weight %.1f)", 
                            season, weight, prev_season, 1.0 - weight)
                features = blend_features(features, previous_features, weight)
                source_summary = f"Blended Data: {int(weight*100)}% Season {season}, {int((1-weight)*100)}% Season {prev_season}"
        elif max_starts >= 5:
            source_summary = f"Full Season {season} Data"

        # Load master list of active drivers/teams from the current season snapshot
        active_drivers, active_teams = load_current_entry_list(Path("data/raw/fastf1"), target_season)
        
        # If no active data found for current season, fallback to features list (old behavior)
        if not active_drivers:
            LOGGER.warning("No active entry list found for season %s. Falling back to feature-based list.", target_season)
            active_drivers = {str(row.get("driver") or ""): str(row.get("team") or "") for row in features.get("drivers", []) if isinstance(row, dict)}
            active_teams = sorted(list(set(active_drivers.values())))

        signals = load_signals(Path(args.signals_dir))
        guardrails = load_signal_guardrails(Path(args.guardrails_config))
        wet_by_team, safety_by_team, penalties_by_team = aggregate_optional_signal_indexes(signals, guardrails=guardrails)

        drivers = compute_driver_ratings(features, wet_by_team, active_drivers)
        teams = compute_team_ratings(features, active_teams)
        strategy = compute_strategy_scores(features, safety_by_team, active_teams)
        reliability = compute_reliability_scores(features, penalties_by_team, active_teams)

        metadata = {
            "season": season,
            "source_features": features_path.as_posix(),
            "source_summary": source_summary,
            "inputs_hash": stable_hash_json({"features": features, "signals": signals}),
        }

        models_dir = Path(args.models_dir)
        models_dir.mkdir(parents=True, exist_ok=True)

        write_json(models_dir / "driver_ratings.json", {**metadata, **drivers})
        write_json(models_dir / "team_ratings.json", {**metadata, **teams})
        write_json(models_dir / "strategy_scores.json", {**metadata, **strategy})
        write_json(models_dir / "reliability_scores.json", {**metadata, **reliability})
    except Exception as exc:
        LOGGER.error("update_ratings failed: %s", exc)
        return 1

    LOGGER.info("Updated rating files in %s", Path(args.models_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
