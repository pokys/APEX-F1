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

    mapping: dict[str, str] = {}
    teams: set[str] = set()

    # Prefer data from completed events/sessions if available
    events = payload.get("events", [])
    for event in events:
        for session in event.get("sessions", []):
            for res in session.get("results", []):
                d = str(res.get("abbreviation") or "").strip().upper()
                t = str(res.get("team_name") or "").strip()
                if d and t:
                    mapping[d] = t
                    teams.add(t)

    # Fallback to calendar if no sessions yet (though less reliable for driver names)
    return mapping, sorted(list(teams))


def compute_driver_ratings(features: dict[str, Any], wet_by_team: dict[str, float], active_drivers: dict[str, str]) -> dict[str, Any]:
    rows = features.get("drivers", [])
    if not isinstance(rows, list):
        rows = []

    # Map existing feature data for quick lookup
    feature_map = {str(row.get("driver") or "").strip().upper(): row for row in rows if isinstance(row, dict)}

    # Teammate deltas based on average race position.
    by_team: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        team_key = slug(str(row.get("team") or ""))
        driver_name = str(row.get("driver") or "").strip().upper()
        # Only include in teammate delta calculation if they are in the feature set
        race_avg = safe_float(row.get("race_avg_position"))
        if not team_key or not driver_name or race_avg is None:
            continue
        by_team.setdefault(team_key, []).append((driver_name, race_avg))

    teammate_delta: dict[str, float] = {}
    for team_key, pairs in by_team.items():
        if len(pairs) < 2:
            for driver_name, _ in pairs:
                teammate_delta[driver_name] = 0.0
            continue
        team_mean = statistics.fmean(v for _, v in pairs)
        for driver_name, race_avg in pairs:
            teammate_delta[driver_name] = round(team_mean - race_avg, 6)

    payload_rows: list[dict[str, Any]] = []
    # Use active_drivers as the master list
    for driver_name, team_name in sorted(active_drivers.items()):
        team_key = slug(team_name)
        row = feature_map.get(driver_name, {})

        race_avg = safe_float(row.get("race_avg_position"))
        q_avg = safe_float(row.get("qualifying_avg_position"))
        practice_avg = safe_float(row.get("practice_avg_position"))
        sprint_q_avg = safe_float(row.get("sprint_qualifying_avg_position"))
        q_phase = safe_float(row.get("qualifying_phase_depth"))
        sprint_q_phase = safe_float(row.get("sprint_qualifying_phase_depth"))
        dnf_rate = safe_float(row.get("dnf_rate"))
        starts = safe_float(row.get("starts")) or 0.0
        recent_race_form = safe_float(row.get("race_form_last3"))
        signal_conf = safe_float(row.get("signal_driver_confidence_delta")) or 0.0

        # Baseline for rookies or missing data
        t_delta = teammate_delta.get(driver_name, 0.0)
        sample_scale = clamp(starts / 5.0, 0.0, 1.0)
        teammate_component = 50.0 + 14.0 * clamp(t_delta, -2.0, 2.0) * sample_scale
        consistency_component = 75.0 - 40.0 * clamp((dnf_rate if dnf_rate is not None else 0.15), 0.0, 1.0)
        wet_index = wet_by_team.get(team_key, 0.5)
        wet_component = 35.0 + 30.0 * clamp(wet_index, 0.0, 1.0)
        base_quali_component = 80.0 - 2.8 * clamp((q_avg if q_avg is not None else 12.0), 1.0, 20.0)
        race_form_component = 82.0 - 2.6 * clamp((recent_race_form if recent_race_form is not None else (race_avg if race_avg is not None else 12.0)), 1.0, 20.0)
        practice_component = 78.0 - 2.4 * clamp((practice_avg if practice_avg is not None else 12.0), 1.0, 20.0)
        sprint_quali_component = 78.0 - 2.6 * clamp((sprint_q_avg if sprint_q_avg is not None else (q_avg if q_avg is not None else 12.0)), 1.0, 20.0)
        phase_score = q_phase if q_phase is not None else sprint_q_phase if sprint_q_phase is not None else 0.5
        progression_component = 45.0 + 18.0 * clamp(phase_score, 0.0, 1.0)
        qualifying_component = (
            0.35 * base_quali_component
            + 0.25 * race_form_component
            + 0.15 * practice_component
            + 0.15 * sprint_quali_component
            + 0.10 * progression_component
        )

        # Small adjustment for rookies to not be absolute last if they show promise in signals
        if driver_name not in feature_map:
             # Default rookie rating baseline
             rating = 68.0 + 5.0 * clamp(signal_conf, -1.0, 1.0)
        else:
            signal_component = 5.0 * clamp(signal_conf, -1.0, 1.0)
            rating = (
                0.20 * teammate_component
                + 0.25 * consistency_component
                + 0.20 * wet_component
                + 0.35 * qualifying_component
                + signal_component
            )

        rating = round(clamp(rating, 0.0, 100.0), 6)

        payload_rows.append(
            {
                "driver": driver_name,
                "team": team_name,
                "driver_rating": rating,
                "components": {
                    "teammate_delta_performance": round(teammate_component, 6),
                    "consistency": round(consistency_component, 6),
                    "wet_performance_index": round(wet_component, 6),
                    "qualifying_pace": round(qualifying_component, 6),
                    "recent_race_form": round(race_form_component, 6),
                    "weekend_practice_pace": round(practice_component, 6),
                    "qualifying_progression": round(progression_component, 6),
                },
            }
        )

    return {"drivers": payload_rows}


def compute_team_ratings(features: dict[str, Any], active_teams: list[str]) -> dict[str, Any]:
    rows = features.get("teams", [])
    if not isinstance(rows, list):
        rows = []

    feature_map = {str(row.get("team") or "").strip(): row for row in rows if isinstance(row, dict)}

    q_values = [
        safe_float(row.get("qualifying_avg_position"))
        for row in rows
        if isinstance(row, dict) and safe_float(row.get("qualifying_avg_position")) is not None
    ]
    field_q_mean = statistics.fmean(q_values) if q_values else 10.5

    payload_rows: list[dict[str, Any]] = []
    for team_name in sorted(active_teams):
        row = feature_map.get(team_name, {})

        q_avg = safe_float(row.get("qualifying_avg_position"))
        practice_avg = safe_float(row.get("practice_avg_position"))
        sprint_q_avg = safe_float(row.get("sprint_qualifying_avg_position"))
        q_phase = safe_float(row.get("qualifying_phase_depth"))
        race_avg = safe_float(row.get("race_avg_position"))
        upgrade_score = safe_float(row.get("signal_upgrade_score"))

        q_inputs = [value for value in (q_avg, sprint_q_avg, practice_avg) if value is not None]
        q_reference = statistics.fmean(q_inputs) if q_inputs else field_q_mean
        q_gap_proxy = 50.0 + 7.0 * clamp(field_q_mean - q_reference, -6.0, 6.0)
        sector_dominance = 52.0 + 5.5 * clamp((q_avg if q_avg is not None else 12.0) - (race_avg if race_avg is not None else 12.0), -6.0, 6.0)
        upgrades_impact = 45.0 + 16.0 * clamp((upgrade_score if upgrade_score is not None else 1.0), 0.0, 3.0)
        weekend_pace_proxy = 45.0 + 20.0 * clamp(1.0 - ((practice_avg if practice_avg is not None else 12.0) / 20.0), 0.0, 1.0)
        progression_proxy = 40.0 + 20.0 * clamp((q_phase if q_phase is not None else 0.5), 0.0, 1.0)

        if team_name not in feature_map:
            # Baseline for new teams (e.g. Cadillac)
            rating = 55.0 + upgrades_impact * 0.1
        else:
            rating = 0.35 * q_gap_proxy + 0.20 * sector_dominance + 0.25 * upgrades_impact + 0.10 * weekend_pace_proxy + 0.10 * progression_proxy

        rating = round(clamp(rating, 0.0, 100.0), 6)

        payload_rows.append(
            {
                "team": team_name,
                "team_rating": rating,
                "components": {
                    "qualifying_gap_proxy": round(q_gap_proxy, 6),
                    "sector_dominance": round(sector_dominance, 6),
                    "upgrades_impact": round(upgrades_impact, 6),
                    "weekend_pace_proxy": round(weekend_pace_proxy, 6),
                    "qualifying_progression": round(progression_proxy, 6),
                },
            }
        )

    return {"teams": payload_rows}


def compute_strategy_scores(features: dict[str, Any], safety_by_team: dict[str, float], active_teams: list[str]) -> dict[str, Any]:
    rows = features.get("teams", [])
    if not isinstance(rows, list):
        rows = []

    feature_map = {str(row.get("team") or "").strip(): row for row in rows if isinstance(row, dict)}

    payload_rows: list[dict[str, Any]] = []
    for team_name in sorted(active_teams):
        team_key = slug(team_name)
        row = feature_map.get(team_name, {})

        q_avg = safe_float(row.get("qualifying_avg_position"))
        race_avg = safe_float(row.get("race_avg_position"))
        sprint_q_avg = safe_float(row.get("sprint_qualifying_avg_position"))
        sprint_avg = safe_float(row.get("sprint_avg_position"))
        starts = safe_float(row.get("starts")) or 0.0
        points = safe_float(row.get("points_total")) or 0.0

        delta = (q_avg if q_avg is not None else 12.0) - (race_avg if race_avg is not None else 12.0)
        pit_stop_perf = 50.0 + 8.0 * clamp(delta, -5.0, 5.0)
        strategic_history = 40.0 + 4.0 * clamp((points / max(starts, 1.0)), 0.0, 20.0)
        safety_reaction = 40.0 + 40.0 * clamp(safety_by_team.get(team_key, 0.5), 0.0, 1.0)
        sprint_execution = 50.0 + 9.0 * clamp(
            (sprint_q_avg if sprint_q_avg is not None else 12.0) - (sprint_avg if sprint_avg is not None else (race_avg if race_avg is not None else 12.0)),
            -5.0,
            5.0,
        )

        if team_name not in feature_map:
            score = 50.0 + 10.0 * clamp(safety_by_team.get(team_key, 0.5) - 0.5, -0.5, 0.5)
        else:
            score = 0.30 * pit_stop_perf + 0.35 * strategic_history + 0.20 * safety_reaction + 0.15 * sprint_execution

        score = round(clamp(score, 0.0, 100.0), 6)

        payload_rows.append(
            {
                "team": team_name,
                "strategy_score": score,
                "components": {
                    "pit_stop_performance": round(pit_stop_perf, 6),
                    "strategic_success_history": round(strategic_history, 6),
                    "safety_car_reactions": round(safety_reaction, 6),
                    "sprint_execution": round(sprint_execution, 6),
                },
            }
        )

    return {"teams": payload_rows}


def compute_reliability_scores(features: dict[str, Any], penalties_by_team: dict[str, float], active_teams: list[str]) -> dict[str, Any]:
    rows = features.get("teams", [])
    if not isinstance(rows, list):
        rows = []

    feature_map = {str(row.get("team") or "").strip(): row for row in rows if isinstance(row, dict)}

    payload_rows: list[dict[str, Any]] = []
    for team_name in sorted(active_teams):
        team_key = slug(team_name)
        row = feature_map.get(team_name, {})

        dnf_rate = safe_float(row.get("dnf_rate"))
        signal_rel = safe_float(row.get("signal_reliability_concern"))
        penalty_idx = penalties_by_team.get(team_key, 0.0)

        dnf_component = 85.0 - 70.0 * clamp((dnf_rate if dnf_rate is not None else 0.1), 0.0, 1.0)
        pu_component = 80.0 - 55.0 * clamp((signal_rel if signal_rel is not None else 0.2), 0.0, 1.0)
        penalty_component = 85.0 - 45.0 * clamp(penalty_idx, 0.0, 1.0)

        if team_name not in feature_map:
            score = 70.0 - 20.0 * clamp(penalty_idx, 0.0, 1.0)
        else:
            score = 0.45 * dnf_component + 0.35 * pu_component + 0.20 * penalty_component

        score = round(clamp(score, 0.0, 100.0), 6)

        payload_rows.append(
            {
                "team": team_name,
                "reliability_score": score,
                "components": {
                    "dnf_history": round(dnf_component, 6),
                    "power_unit_reliability": round(pu_component, 6),
                    "new_component_penalties": round(penalty_component, 6),
                },
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
            "practice_avg_position", "fp1_avg_position", "fp2_avg_position", "fp3_avg_position",
            "sprint_qualifying_avg_position", "sprint_avg_position",
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
