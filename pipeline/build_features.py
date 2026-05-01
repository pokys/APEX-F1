#!/usr/bin/env python3
"""
Build deterministic feature tables from hard race data and processed signals.

Inputs:
- data/raw/fastf1/season_<year>.json
- knowledge/processed/*.json

Output:
- data/processed/features_season_<year>.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("build_features")

UPGRADE_MAGNITUDE_SCORE = {"minor": 1.0, "medium": 2.0, "major": 3.0}
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
    "caps": {
        "upgrade": {"baseline": 1.0, "max_delta": 0.8},
        "reliability": {"baseline": 0.2, "max_delta": 0.25},
        "driver_confidence": {"baseline": 0.0, "max_delta": 0.35},
    },
}
DEFAULT_RECENCY_CONFIG = {
    "half_life_events": {
        "race": 4.0,
        "qualifying": 4.0,
        "sprint": 3.0,
        "sprint_qualifying": 3.0,
        "practice": 2.0,
        "fp1": 2.0,
        "fp2": 2.0,
        "fp3": 2.0,
    },
    "stale_threshold_days": 21,
    "minimum_effective_sample": 1.5,
}
SEASON_FILE_RE = re.compile(r"^season_(\d{4})\.json$")
PRACTICE_CODES = {"FP1", "FP2", "FP3"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build deterministic feature dataset.")
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Season year. If omitted, inferred from FastF1 snapshot.",
    )
    parser.add_argument(
        "--fastf1-input",
        default="data/raw/fastf1",
        help="FastF1 snapshot file or directory.",
    )
    parser.add_argument(
        "--signals-dir",
        default="knowledge/processed",
        help="Directory with processed signal JSON files.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/processed",
        help="Output directory for processed feature files.",
    )
    parser.add_argument(
        "--guardrails-config",
        default="config/signal_guardrails.json",
        help="Signal guardrails configuration JSON path.",
    )
    parser.add_argument(
        "--recency-config",
        default="config/recency.json",
        help="Recency-weighting configuration JSON path.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--allow-missing-fastf1",
        action="store_true",
        help="Exit successfully when FastF1 snapshot is not available yet.",
    )
    return parser.parse_args()


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
            num = to_float(value)
            if num is None:
                continue
            normalized[key.strip().lower()] = max(0.0, min(1.0, num))
        if normalized:
            merged["source_credibility"] = normalized

    for key in ("default_source_credibility", "source_confidence_floor", "echo_decay"):
        num = to_float(raw.get(key))
        if num is not None:
            merged[key] = max(0.0, min(1.0, num))

    raw_caps = raw.get("caps")
    if isinstance(raw_caps, dict):
        caps = merged["caps"]
        for cap_key in ("upgrade", "reliability", "driver_confidence"):
            cap_raw = raw_caps.get(cap_key)
            if not isinstance(cap_raw, dict):
                continue
            baseline = to_float(cap_raw.get("baseline"))
            max_delta = to_float(cap_raw.get("max_delta"))
            if baseline is not None:
                caps[cap_key]["baseline"] = baseline
            if max_delta is not None:
                caps[cap_key]["max_delta"] = max(0.0, max_delta)
    return merged


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def stable_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.fmean(values), 6)


def recency_weighted_mean(pairs: list[tuple[int, float]], half_life: float) -> tuple[float | None, float]:
    """Exponential-decay weighted mean ranked by event index.

    pairs: list of (event_index, value). The most recent event_index gets
    weight 1.0; older events decay as 0.5 ** (rank / half_life).

    Returns (mean, effective_sample_size). When pairs is empty returns (None, 0.0).
    Effective sample size uses the Kish formula (sum w)^2 / sum w^2.
    """
    if not pairs:
        return None, 0.0
    max_idx = max(idx for idx, _ in pairs)
    half = max(float(half_life), 1e-6)
    decay = 0.5 ** (1.0 / half)
    weighted_sum = 0.0
    weight_sum = 0.0
    weight_sq_sum = 0.0
    for idx, value in pairs:
        rank = max_idx - idx
        weight = decay ** rank
        weighted_sum += weight * value
        weight_sum += weight
        weight_sq_sum += weight * weight
    if weight_sum <= 0.0:
        return None, 0.0
    mean = weighted_sum / weight_sum
    ess = (weight_sum * weight_sum) / weight_sq_sum if weight_sq_sum > 0 else 0.0
    return round(mean, 6), round(ess, 6)


def load_recency_config(path: Path) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_RECENCY_CONFIG))
    if not path.exists():
        return merged
    try:
        raw = load_json(path)
    except Exception as exc:
        LOGGER.warning("Could not parse recency config %s: %s", path, exc)
        return merged
    if not isinstance(raw, dict):
        return merged

    half_lives = raw.get("half_life_events")
    if isinstance(half_lives, dict):
        normalized: dict[str, float] = {}
        for key, value in half_lives.items():
            num = to_float(value)
            if num is None or num <= 0:
                continue
            normalized[str(key).strip().lower()] = num
        if normalized:
            base = dict(merged["half_life_events"])
            base.update(normalized)
            merged["half_life_events"] = base

    stale = to_float(raw.get("stale_threshold_days"))
    if stale is not None and stale >= 0:
        merged["stale_threshold_days"] = stale

    min_ess = to_float(raw.get("minimum_effective_sample"))
    if min_ess is not None and min_ess >= 0:
        merged["minimum_effective_sample"] = min_ess

    return merged


def half_life_for(recency_config: dict[str, Any], source_key: str) -> float:
    half_lives = recency_config.get("half_life_events", {})
    if isinstance(half_lives, dict):
        candidate = to_float(half_lives.get(source_key))
        if candidate is not None and candidate > 0:
            return candidate
    default = DEFAULT_RECENCY_CONFIG["half_life_events"].get(source_key, 4.0)
    return float(default)


def slug(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


def is_race_finish(status: str | None) -> bool:
    if not status:
        return False
    text = status.strip().lower()
    return text.startswith("finished") or text.startswith("+")


def season_from_snapshot_path(path: Path) -> int | None:
    match = SEASON_FILE_RE.match(path.name)
    if not match:
        return None
    return int(match.group(1))


def snapshot_has_results(path: Path) -> bool:
    try:
        payload = load_json(path)
    except Exception:
        return False
    events = payload.get("events")
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, dict):
            continue
        sessions = event.get("sessions")
        if not isinstance(sessions, list):
            continue
        for session in sessions:
            if not isinstance(session, dict):
                continue
            results = session.get("results")
            if isinstance(results, list) and len(results) > 0:
                return True
    return False


def choose_fastf1_snapshot(fastf1_input: Path, season: int | None) -> Path:
    if fastf1_input.is_file():
        return fastf1_input

    if not fastf1_input.exists():
        raise FileNotFoundError(f"FastF1 input path does not exist: {fastf1_input}")

    candidates = sorted(fastf1_input.glob("season_*.json"))
    if not candidates:
        raise FileNotFoundError(f"No FastF1 snapshots found in: {fastf1_input}")

    if season is not None:
        expected = fastf1_input / f"season_{season}.json"
        if not expected.exists():
            raise FileNotFoundError(f"FastF1 snapshot not found: {expected}")
        if snapshot_has_results(expected):
            return expected

        fallback_candidates = sorted(
            (p for p in candidates if (season_from_snapshot_path(p) or 0) < season),
            key=lambda p: season_from_snapshot_path(p) or -1,
            reverse=True,
        )
        for path in fallback_candidates:
            if snapshot_has_results(path):
                LOGGER.warning(
                    "Requested season %s has no completed sessions; falling back to season %s snapshot %s",
                    season,
                    season_from_snapshot_path(path),
                    path,
                )
                return path
        return expected

    by_season_desc = sorted(
        candidates,
        key=lambda p: season_from_snapshot_path(p) or -1,
        reverse=True,
    )
    for path in by_season_desc:
        if snapshot_has_results(path):
            return path
    return by_season_desc[0]


def qualifying_phase_depth(row: dict[str, Any]) -> float | None:
    depth = 0
    for key in ("q1", "q2", "q3"):
        if row.get(key) is not None:
            depth += 1
    if depth <= 0:
        return None
    return depth / 3.0


def normalize_signals(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        signals = raw.get("signals")
        if isinstance(signals, list):
            return [x for x in signals if isinstance(x, dict)]
        return [raw]
    return []


def collect_signals(signals_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not signals_dir.exists():
        return [], []

    signal_files = sorted(signals_dir.glob("*.json"))
    all_signals: list[dict[str, Any]] = []
    used_files: list[str] = []
    for path in signal_files:
        try:
            parsed = load_json(path)
        except json.JSONDecodeError as exc:
            LOGGER.warning("Skipping invalid signal JSON %s: %s", path, exc)
            continue
        normalized = normalize_signals(parsed)
        if not normalized:
            continue
        all_signals.extend(normalized)
        used_files.append(str(path.as_posix()))
    return all_signals, used_files


def signal_weight(signal: dict[str, Any], guardrails: dict[str, Any]) -> float:
    source_name = str(signal.get("source_name") or "").strip().lower()
    source_credibility = guardrails.get("source_credibility", {})
    if not isinstance(source_credibility, dict):
        source_credibility = {}
    credibility = to_float(source_credibility.get(source_name))
    if credibility is None:
        credibility = to_float(guardrails.get("default_source_credibility"))
    if credibility is None:
        credibility = 0.45
    credibility = max(0.0, min(1.0, credibility))
    source_confidence = to_float(signal.get("source_confidence"))
    if source_confidence is None:
        source_confidence = 0.5
    source_confidence = max(0.0, min(1.0, source_confidence))
    floor = to_float(guardrails.get("source_confidence_floor"))
    if floor is None:
        floor = 0.2
    if source_confidence < max(0.0, min(1.0, floor)):
        return 0.0
    return source_confidence * credibility


def signal_fingerprint(signal: dict[str, Any]) -> str:
    team_key = slug(str(signal.get("team") or ""))
    driver_key = slug(str(signal.get("driver") or signal.get("driver_name") or ""))
    if signal.get("upgrade_detected"):
        magnitude = str(signal.get("upgrade_magnitude") or "").strip().lower()
        component = slug(str(signal.get("upgrade_component") or "unknown"))
        return f"upgrade|{team_key}|{component}|{magnitude}"
    reliability = to_float(signal.get("reliability_concern"))
    if reliability is not None:
        return f"reliability|{team_key}|{round(max(0.0, min(1.0, reliability)), 1)}"
    confidence = to_float(signal.get("driver_confidence_change"))
    if confidence is not None:
        sign = 1 if confidence > 0 else -1 if confidence < 0 else 0
        return f"driver_conf|{driver_key}|{sign}|{round(abs(confidence), 1)}"
    return f"generic|{team_key}|{driver_key}"


def capped_soft_signal(value: float, baseline: float, max_delta: float, lo: float, hi: float) -> float:
    delta = max(-max_delta, min(max_delta, value - baseline))
    return max(lo, min(hi, baseline + delta))


def aggregate_signals(signals: list[dict[str, Any]], guardrails: dict[str, Any]) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    team_agg: dict[str, dict[str, float]] = {}
    driver_agg: dict[str, dict[str, float]] = {}
    echo_decay = to_float(guardrails.get("echo_decay"))
    if echo_decay is None:
        echo_decay = 0.6
    echo_decay = max(0.0, min(1.0, echo_decay))
    echo_counts: dict[str, int] = {}

    for signal in signals:
        weight = signal_weight(signal, guardrails)
        if weight <= 0:
            continue
        fingerprint = signal_fingerprint(signal)
        seen_count = echo_counts.get(fingerprint, 0)
        weight *= echo_decay**seen_count
        echo_counts[fingerprint] = seen_count + 1
        if weight <= 0:
            continue

        team_key = slug(str(signal.get("team") or ""))
        if team_key:
            team_state = team_agg.setdefault(
                team_key,
                {
                    "weight_sum": 0.0,
                    "upgrade_weighted_sum": 0.0,
                    "reliability_weighted_sum": 0.0,
                    "signal_count": 0.0,
                },
            )
            team_state["weight_sum"] += weight
            team_state["signal_count"] += 1.0

            if signal.get("upgrade_detected"):
                magnitude = str(signal.get("upgrade_magnitude") or "").strip().lower()
                score = UPGRADE_MAGNITUDE_SCORE.get(magnitude, 0.0)
                team_state["upgrade_weighted_sum"] += weight * score

            reliability = to_float(signal.get("reliability_concern"))
            if reliability is not None:
                reliability = max(0.0, min(1.0, reliability))
                team_state["reliability_weighted_sum"] += weight * reliability

        driver_key = slug(str(signal.get("driver") or signal.get("driver_name") or ""))
        if driver_key:
            driver_state = driver_agg.setdefault(
                driver_key,
                {"weight_sum": 0.0, "confidence_weighted_sum": 0.0, "signal_count": 0.0},
            )
            driver_state["weight_sum"] += weight
            driver_state["signal_count"] += 1.0

            delta = to_float(signal.get("driver_confidence_change"))
            if delta is not None:
                delta = max(-1.0, min(1.0, delta))
                driver_state["confidence_weighted_sum"] += weight * delta

    return team_agg, driver_agg


def _last_n_values(pairs: list[tuple[int, float]], n: int) -> list[float]:
    if not pairs or n <= 0:
        return []
    ordered = sorted(pairs, key=lambda item: item[0])
    return [value for _, value in ordered[-n:]]


def build_features(
    fastf1_snapshot: dict[str, Any],
    signals: list[dict[str, Any]],
    guardrails: dict[str, Any],
    recency_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    season = int(fastf1_snapshot.get("season"))
    events = fastf1_snapshot.get("events", [])
    if not isinstance(events, list):
        raise ValueError("FastF1 snapshot has invalid events format.")

    if recency_config is None:
        recency_config = json.loads(json.dumps(DEFAULT_RECENCY_CONFIG))

    drivers: dict[str, dict[str, Any]] = {}
    teams: dict[str, dict[str, Any]] = {}

    def ensure_driver(driver_code: str, team_name: str) -> dict[str, Any]:
        driver_key = slug(driver_code)
        team_key = slug(team_name)
        return drivers.setdefault(
            driver_key,
            {
                "driver": driver_code,
                "team": team_name,
                "team_key": team_key,
                "race_positions": [],
                "qualifying_positions": [],
                "practice_positions": [],
                "fp1_positions": [],
                "fp2_positions": [],
                "fp3_positions": [],
                "sprint_qualifying_positions": [],
                "sprint_positions": [],
                "qualifying_phase_depths": [],
                "sprint_qualifying_phase_depths": [],
                "starts": 0,
                "dnfs": 0,
                "points_total": 0.0,
            },
        )

    def ensure_team(team_name: str) -> dict[str, Any]:
        team_key = slug(team_name)
        return teams.setdefault(
            team_key,
            {
                "team": team_name,
                "race_positions": [],
                "qualifying_positions": [],
                "practice_positions": [],
                "fp1_positions": [],
                "fp2_positions": [],
                "fp3_positions": [],
                "sprint_qualifying_positions": [],
                "sprint_positions": [],
                "qualifying_phase_depths": [],
                "sprint_qualifying_phase_depths": [],
                "starts": 0,
                "dnfs": 0,
                "points_total": 0.0,
            },
        )

    event_idx = -1
    for event in events:
        if not isinstance(event, dict):
            continue
        sessions = event.get("sessions", [])
        if not isinstance(sessions, list):
            continue
        event_idx += 1

        for session in sessions:
            if not isinstance(session, dict):
                continue
            code = str(session.get("session_code") or "").upper()
            results = session.get("results")
            if not isinstance(results, list):
                continue
            for row in results:
                if not isinstance(row, dict):
                    continue
                driver_code = str(row.get("abbreviation") or row.get("full_name") or "").strip()
                team_name = str(row.get("team_name") or "").strip()
                if not driver_code or not team_name:
                    continue

                driver_state = ensure_driver(driver_code, team_name)
                team_state = ensure_team(team_name)
                position = to_float(row.get("position"))
                phase_depth = qualifying_phase_depth(row)

                if code == "R":
                    if position is not None:
                        driver_state["race_positions"].append((event_idx, position))
                        team_state["race_positions"].append((event_idx, position))
                    driver_state["starts"] += 1
                    team_state["starts"] += 1
                    if not is_race_finish(str(row.get("status") or "")):
                        driver_state["dnfs"] += 1
                        team_state["dnfs"] += 1
                    points = to_float(row.get("points"))
                    if points is not None:
                        driver_state["points_total"] += points
                        team_state["points_total"] += points
                elif code == "Q":
                    if position is not None:
                        driver_state["qualifying_positions"].append((event_idx, position))
                        team_state["qualifying_positions"].append((event_idx, position))
                    if phase_depth is not None:
                        driver_state["qualifying_phase_depths"].append((event_idx, phase_depth))
                        team_state["qualifying_phase_depths"].append((event_idx, phase_depth))
                elif code == "SQ":
                    if position is not None:
                        driver_state["sprint_qualifying_positions"].append((event_idx, position))
                        team_state["sprint_qualifying_positions"].append((event_idx, position))
                    if phase_depth is not None:
                        driver_state["sprint_qualifying_phase_depths"].append((event_idx, phase_depth))
                        team_state["sprint_qualifying_phase_depths"].append((event_idx, phase_depth))
                elif code == "S":
                    if position is not None:
                        driver_state["sprint_positions"].append((event_idx, position))
                        team_state["sprint_positions"].append((event_idx, position))
                elif code in PRACTICE_CODES and position is not None:
                    driver_state["practice_positions"].append((event_idx, position))
                    team_state["practice_positions"].append((event_idx, position))
                    driver_state[f"{code.lower()}_positions"].append((event_idx, position))
                    team_state[f"{code.lower()}_positions"].append((event_idx, position))

    team_signal_agg, driver_signal_agg = aggregate_signals(signals, guardrails=guardrails)
    caps = guardrails.get("caps", {})
    if not isinstance(caps, dict):
        caps = {}

    hl_race = half_life_for(recency_config, "race")
    hl_qualifying = half_life_for(recency_config, "qualifying")
    hl_sprint = half_life_for(recency_config, "sprint")
    hl_sprint_q = half_life_for(recency_config, "sprint_qualifying")
    hl_practice = half_life_for(recency_config, "practice")
    hl_fp1 = half_life_for(recency_config, "fp1")
    hl_fp2 = half_life_for(recency_config, "fp2")
    hl_fp3 = half_life_for(recency_config, "fp3")

    def driver_or_team_metrics(state: dict[str, Any]) -> dict[str, Any]:
        race_avg, race_ess = recency_weighted_mean(state["race_positions"], hl_race)
        q_avg, q_ess = recency_weighted_mean(state["qualifying_positions"], hl_qualifying)
        practice_avg, _ = recency_weighted_mean(state["practice_positions"], hl_practice)
        fp1_avg, _ = recency_weighted_mean(state["fp1_positions"], hl_fp1)
        fp2_avg, _ = recency_weighted_mean(state["fp2_positions"], hl_fp2)
        fp3_avg, _ = recency_weighted_mean(state["fp3_positions"], hl_fp3)
        sprint_q_avg, _ = recency_weighted_mean(state["sprint_qualifying_positions"], hl_sprint_q)
        sprint_avg, _ = recency_weighted_mean(state["sprint_positions"], hl_sprint)
        q_phase, _ = recency_weighted_mean(state["qualifying_phase_depths"], hl_qualifying)
        sprint_q_phase, _ = recency_weighted_mean(state["sprint_qualifying_phase_depths"], hl_sprint_q)
        return {
            "race_avg_position": race_avg,
            "qualifying_avg_position": q_avg,
            "practice_avg_position": practice_avg,
            "fp1_avg_position": fp1_avg,
            "fp2_avg_position": fp2_avg,
            "fp3_avg_position": fp3_avg,
            "sprint_qualifying_avg_position": sprint_q_avg,
            "sprint_avg_position": sprint_avg,
            "qualifying_phase_depth": q_phase,
            "sprint_qualifying_phase_depth": sprint_q_phase,
            "race_effective_starts": race_ess,
            "qualifying_effective_starts": q_ess,
        }

    driver_rows: list[dict[str, Any]] = []
    for key in sorted(drivers.keys()):
        state = drivers[key]
        race_positions = state["race_positions"]

        starts = int(state["starts"])
        dnfs = int(state["dnfs"])
        signal = driver_signal_agg.get(key, {})
        signal_weight = signal.get("weight_sum", 0.0)
        confidence_delta = None
        if signal_weight > 0:
            raw_conf_delta = signal.get("confidence_weighted_sum", 0.0) / signal_weight
            cap_cfg = caps.get("driver_confidence", {}) if isinstance(caps.get("driver_confidence"), dict) else {}
            baseline = to_float(cap_cfg.get("baseline"))
            max_delta = to_float(cap_cfg.get("max_delta"))
            if baseline is None:
                baseline = 0.0
            if max_delta is None:
                max_delta = 0.35
            confidence_delta = round(capped_soft_signal(raw_conf_delta, baseline, max_delta, -1.0, 1.0), 6)

        metrics = driver_or_team_metrics(state)
        driver_rows.append(
            {
                "driver": state["driver"],
                "team": state["team"],
                "race_avg_position": metrics["race_avg_position"],
                "race_form_last3": stable_mean(_last_n_values(race_positions, 3)),
                "qualifying_avg_position": metrics["qualifying_avg_position"],
                "practice_avg_position": metrics["practice_avg_position"],
                "fp1_avg_position": metrics["fp1_avg_position"],
                "fp2_avg_position": metrics["fp2_avg_position"],
                "fp3_avg_position": metrics["fp3_avg_position"],
                "sprint_qualifying_avg_position": metrics["sprint_qualifying_avg_position"],
                "sprint_avg_position": metrics["sprint_avg_position"],
                "qualifying_phase_depth": metrics["qualifying_phase_depth"],
                "sprint_qualifying_phase_depth": metrics["sprint_qualifying_phase_depth"],
                "starts": starts,
                "race_effective_starts": metrics["race_effective_starts"],
                "qualifying_effective_starts": metrics["qualifying_effective_starts"],
                "dnf_rate": round(dnfs / starts, 6) if starts else None,
                "points_total": round(state["points_total"], 6),
                "signal_driver_confidence_delta": confidence_delta,
                "signal_count": int(signal.get("signal_count", 0.0)),
            }
        )

    team_rows: list[dict[str, Any]] = []
    for key in sorted(teams.keys()):
        state = teams[key]
        starts = int(state["starts"])
        dnfs = int(state["dnfs"])
        signal = team_signal_agg.get(key, {})
        signal_weight = signal.get("weight_sum", 0.0)

        upgrade_score = None
        reliability_concern = None
        if signal_weight > 0:
            raw_upgrade = signal.get("upgrade_weighted_sum", 0.0) / signal_weight
            raw_rel = signal.get("reliability_weighted_sum", 0.0) / signal_weight
            up_cfg = caps.get("upgrade", {}) if isinstance(caps.get("upgrade"), dict) else {}
            rel_cfg = caps.get("reliability", {}) if isinstance(caps.get("reliability"), dict) else {}
            up_baseline = to_float(up_cfg.get("baseline"))
            up_max_delta = to_float(up_cfg.get("max_delta"))
            rel_baseline = to_float(rel_cfg.get("baseline"))
            rel_max_delta = to_float(rel_cfg.get("max_delta"))
            if up_baseline is None:
                up_baseline = 1.0
            if up_max_delta is None:
                up_max_delta = 0.8
            if rel_baseline is None:
                rel_baseline = 0.2
            if rel_max_delta is None:
                rel_max_delta = 0.25
            upgrade_score = round(capped_soft_signal(raw_upgrade, up_baseline, up_max_delta, 0.0, 3.0), 6)
            reliability_concern = round(capped_soft_signal(raw_rel, rel_baseline, rel_max_delta, 0.0, 1.0), 6)

        metrics = driver_or_team_metrics(state)
        team_rows.append(
            {
                "team": state["team"],
                "race_avg_position": metrics["race_avg_position"],
                "qualifying_avg_position": metrics["qualifying_avg_position"],
                "practice_avg_position": metrics["practice_avg_position"],
                "fp1_avg_position": metrics["fp1_avg_position"],
                "fp2_avg_position": metrics["fp2_avg_position"],
                "fp3_avg_position": metrics["fp3_avg_position"],
                "sprint_qualifying_avg_position": metrics["sprint_qualifying_avg_position"],
                "sprint_avg_position": metrics["sprint_avg_position"],
                "qualifying_phase_depth": metrics["qualifying_phase_depth"],
                "sprint_qualifying_phase_depth": metrics["sprint_qualifying_phase_depth"],
                "starts": starts,
                "race_effective_starts": metrics["race_effective_starts"],
                "qualifying_effective_starts": metrics["qualifying_effective_starts"],
                "dnf_rate": round(dnfs / starts, 6) if starts else None,
                "points_total": round(state["points_total"], 6),
                "signal_upgrade_score": upgrade_score,
                "signal_reliability_concern": reliability_concern,
                "signal_count": int(signal.get("signal_count", 0.0)),
            }
        )

    return {
        "season": season,
        "source": "build_features",
        "drivers": driver_rows,
        "teams": team_rows,
        "recency_config": {
            "half_life_events": dict(recency_config.get("half_life_events", {})),
        },
    }


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        fastf1_path = choose_fastf1_snapshot(Path(args.fastf1_input), args.season)
    except FileNotFoundError as exc:
        if args.allow_missing_fastf1:
            LOGGER.warning("Skipping feature build: %s", exc)
            return 0
        LOGGER.error("build_features failed: %s", exc)
        return 1

    try:
        fastf1_snapshot = load_json(fastf1_path)
        season = int(fastf1_snapshot.get("season"))
        if args.season is not None and season != args.season:
            LOGGER.warning(
                "Feature build used season %s snapshot for requested season %s due to missing completed-session data.",
                season,
                args.season,
            )

        signals, signal_files = collect_signals(Path(args.signals_dir))
        guardrails = load_signal_guardrails(Path(args.guardrails_config))
        recency_config = load_recency_config(Path(args.recency_config))
        features = build_features(
            fastf1_snapshot,
            signals,
            guardrails=guardrails,
            recency_config=recency_config,
        )
        features["fastf1_snapshot"] = fastf1_path.as_posix()
        features["signals_files"] = signal_files

        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"features_season_{season}.json"
        output_path.write_text(
            json.dumps(features, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.error("build_features failed: %s", exc)
        return 1

    LOGGER.info("Wrote feature dataset: %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
