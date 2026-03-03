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
SEASON_FILE_RE = re.compile(r"^season_(\d{4})\.json$")


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


def build_features(fastf1_snapshot: dict[str, Any], signals: list[dict[str, Any]], guardrails: dict[str, Any]) -> dict[str, Any]:
    season = int(fastf1_snapshot.get("season"))
    events = fastf1_snapshot.get("events", [])
    if not isinstance(events, list):
        raise ValueError("FastF1 snapshot has invalid events format.")

    drivers: dict[str, dict[str, Any]] = {}
    teams: dict[str, dict[str, Any]] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        sessions = event.get("sessions", [])
        if not isinstance(sessions, list):
            continue

        race_results: list[dict[str, Any]] = []
        qualifying_results: list[dict[str, Any]] = []

        for session in sessions:
            if not isinstance(session, dict):
                continue
            code = str(session.get("session_code") or "").upper()
            results = session.get("results")
            if not isinstance(results, list):
                continue
            if code == "R":
                race_results = [r for r in results if isinstance(r, dict)]
            elif code == "Q":
                qualifying_results = [r for r in results if isinstance(r, dict)]

        for row in race_results:
            driver_code = str(row.get("abbreviation") or row.get("full_name") or "").strip()
            team_name = str(row.get("team_name") or "").strip()
            if not driver_code or not team_name:
                continue
            driver_key = slug(driver_code)
            team_key = slug(team_name)
            if not driver_key or not team_key:
                continue

            driver_state = drivers.setdefault(
                driver_key,
                {
                    "driver": driver_code,
                    "team": team_name,
                    "team_key": team_key,
                    "race_positions": [],
                    "qualifying_positions": [],
                    "starts": 0,
                    "dnfs": 0,
                    "points_total": 0.0,
                },
            )

            position = to_float(row.get("position"))
            if position is not None:
                driver_state["race_positions"].append(position)
            driver_state["starts"] += 1
            if not is_race_finish(str(row.get("status") or "")):
                driver_state["dnfs"] += 1
            points = to_float(row.get("points"))
            if points is not None:
                driver_state["points_total"] += points

            team_state = teams.setdefault(
                team_key,
                {
                    "team": team_name,
                    "race_positions": [],
                    "qualifying_positions": [],
                    "starts": 0,
                    "dnfs": 0,
                    "points_total": 0.0,
                },
            )
            if position is not None:
                team_state["race_positions"].append(position)
            team_state["starts"] += 1
            if not is_race_finish(str(row.get("status") or "")):
                team_state["dnfs"] += 1
            if points is not None:
                team_state["points_total"] += points

        for row in qualifying_results:
            driver_code = str(row.get("abbreviation") or row.get("full_name") or "").strip()
            team_name = str(row.get("team_name") or "").strip()
            if not driver_code or not team_name:
                continue
            driver_key = slug(driver_code)
            team_key = slug(team_name)
            if not driver_key or not team_key:
                continue

            q_pos = to_float(row.get("position"))
            if q_pos is None:
                continue

            if driver_key in drivers:
                drivers[driver_key]["qualifying_positions"].append(q_pos)
            if team_key in teams:
                teams[team_key]["qualifying_positions"].append(q_pos)

    team_signal_agg, driver_signal_agg = aggregate_signals(signals, guardrails=guardrails)
    caps = guardrails.get("caps", {})
    if not isinstance(caps, dict):
        caps = {}

    driver_rows: list[dict[str, Any]] = []
    for key in sorted(drivers.keys()):
        state = drivers[key]
        race_positions = state["race_positions"]
        q_positions = state["qualifying_positions"]

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

        driver_rows.append(
            {
                "driver": state["driver"],
                "team": state["team"],
                "race_avg_position": stable_mean(race_positions),
                "race_form_last3": stable_mean(race_positions[-3:]),
                "qualifying_avg_position": stable_mean(q_positions),
                "starts": starts,
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

        team_rows.append(
            {
                "team": state["team"],
                "race_avg_position": stable_mean(state["race_positions"]),
                "qualifying_avg_position": stable_mean(state["qualifying_positions"]),
                "starts": starts,
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
        features = build_features(fastf1_snapshot, signals, guardrails=guardrails)
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
