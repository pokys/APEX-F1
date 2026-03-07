#!/usr/bin/env python3
"""
Run deterministic Monte Carlo race simulation from model ratings.

Inputs:
- models/driver_ratings.json
- models/team_ratings.json
- models/strategy_scores.json
- models/reliability_scores.json
- optional config/race_config.json

Output:
- outputs/prediction.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import statistics
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("simulate_race")

MIN_SIMULATIONS = 5000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic F1 race Monte Carlo simulation.")
    parser.add_argument("--driver-ratings", default="models/driver_ratings.json", help="Driver ratings JSON path.")
    parser.add_argument("--team-ratings", default="models/team_ratings.json", help="Team ratings JSON path.")
    parser.add_argument("--strategy-scores", default="models/strategy_scores.json", help="Strategy scores JSON path.")
    parser.add_argument("--reliability-scores", default="models/reliability_scores.json", help="Reliability scores JSON path.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Optional race config JSON path.")
    parser.add_argument("--fixed-grid", default=None, help="Comma-separated driver abbreviations for starting grid (overrides config).")
    parser.add_argument("--output", default="outputs/prediction.json", help="Prediction output JSON path.")
    parser.add_argument("--allow-missing-models", action="store_true", help="Exit 0 when model files are missing.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_hash_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_or_default_config(path: Path) -> dict[str, Any]:
    default = {
        "race": "Next GP",
        "race_date": "1970-01-01",
        "generated_at": "1970-01-01T00:00:00Z",
        "seed": 20260303,
        "simulations": 5000,
        "weather": "dry",
        "weather_modifier": 0.0,
        "safety_car_probability": 0.22,
        "overtaking_difficulty": 0.5,
        "track": {
            "tyre_degradation_factor": 0.5,
            "qualifying_noise": 2.6,
            "race_noise": 3.8,
        },
    }
    if not path.exists():
        return default
    data = load_json(path)
    if not isinstance(data, dict):
        return default

    merged = dict(default)
    merged.update({k: v for k, v in data.items() if k != "track"})
    track = dict(default["track"])
    if isinstance(data.get("track"), dict):
        track.update(data["track"])
    merged["track"] = track
    return merged


def build_entries(driver_ratings: dict[str, Any], team_ratings: dict[str, Any], strategy_scores: dict[str, Any], reliability_scores: dict[str, Any]) -> list[dict[str, Any]]:
    team_rating_by_name = {
        str(row.get("team") or ""): safe_float(row.get("team_rating"), 50.0)
        for row in team_ratings.get("teams", [])
        if isinstance(row, dict)
    }
    strategy_by_name = {
        str(row.get("team") or ""): safe_float(row.get("strategy_score"), 50.0)
        for row in strategy_scores.get("teams", [])
        if isinstance(row, dict)
    }
    reliability_by_name = {
        str(row.get("team") or ""): safe_float(row.get("reliability_score"), 60.0)
        for row in reliability_scores.get("teams", [])
        if isinstance(row, dict)
    }

    entries: list[dict[str, Any]] = []
    for row in driver_ratings.get("drivers", []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("driver") or "").strip()
        team = str(row.get("team") or "").strip()
        if not name or not team:
            continue
        entries.append(
            {
                "name": name,
                "team": team,
                "driver_rating": safe_float(row.get("driver_rating"), 50.0),
                "team_rating": team_rating_by_name.get(team, 50.0),
                "strategy_score": strategy_by_name.get(team, 50.0),
                "reliability_score": reliability_by_name.get(team, 60.0),
            }
        )

    entries.sort(key=lambda x: x["name"].lower())
    return entries


def simulate_qualifying(entries: list[dict[str, Any]], rng: random.Random, qualifying_noise: float) -> list[str]:
    scored: list[tuple[str, float]] = []
    for entry in entries:
        base = 0.62 * entry["driver_rating"] + 0.38 * entry["team_rating"]
        variation = rng.gauss(0.0, qualifying_noise)
        scored.append((entry["name"], base + variation))
    scored.sort(key=lambda x: (-x[1], x[0].lower()))
    return [name for name, _ in scored]


def simulate_single_race(
    entries: list[dict[str, Any]],
    grid_order: list[str],
    rng: random.Random,
    safety_car_probability: float,
    overtaking_difficulty: float,
    weather_modifier: float,
    tyre_degradation_factor: float,
    race_noise: float,
) -> dict[str, int]:
    size = len(entries)
    grid_index = {name: idx + 1 for idx, name in enumerate(grid_order)}
    safety_car_active = rng.random() < safety_car_probability

    scored_finish: list[tuple[str, float]] = []
    dnf_drivers: list[str] = []

    for entry in entries:
        name = entry["name"]
        grid_pos = grid_index.get(name, size)
        grid_factor = (size - grid_pos) / max(size - 1, 1)

        base_pace = (
            0.52 * entry["driver_rating"]
            + 0.30 * entry["team_rating"]
            + 0.18 * entry["strategy_score"]
        )
        strategy_noise = rng.gauss(0.0, 1.0 + tyre_degradation_factor)
        tyre_noise = rng.gauss(0.0, 1.2 + 1.8 * tyre_degradation_factor)
        weather_noise = rng.gauss(0.0, 1.0) * weather_modifier
        safety_effect = (rng.gauss(0.0, 1.8) + 0.03 * entry["strategy_score"]) if safety_car_active else 0.0

        start_track_position_advantage = 4.0 * grid_factor * overtaking_difficulty
        overtaking_recovery = 4.5 * (1.0 - overtaking_difficulty) * (entry["driver_rating"] / 100.0)
        pure_noise = rng.gauss(0.0, race_noise)

        reliability_fail_prob = clamp(0.01 + (100.0 - entry["reliability_score"]) / 170.0, 0.01, 0.35)
        if rng.random() < reliability_fail_prob:
            dnf_drivers.append(name)
            continue

        race_score = (
            base_pace
            + strategy_noise
            + tyre_noise
            + weather_noise
            + safety_effect
            + start_track_position_advantage
            + overtaking_recovery
            + pure_noise
        )
        scored_finish.append((name, race_score))

    scored_finish.sort(key=lambda x: (-x[1], x[0].lower()))

    finish_positions: dict[str, int] = {}
    for idx, (name, _) in enumerate(scored_finish, start=1):
        finish_positions[name] = idx

    dnf_position = size + 1
    for name in sorted(dnf_drivers):
        finish_positions[name] = dnf_position

    return finish_positions


def temperature_scale_distribution(prob_map: dict[str, float], temperature: float) -> dict[str, float]:
    eps = 1e-12
    power = 1.0 / max(temperature, eps)
    scaled = {k: max(v, eps) ** power for k, v in prob_map.items()}
    total = sum(scaled.values())
    if total <= 0:
        uniform = 1.0 / max(len(prob_map), 1)
        return {k: uniform for k in prob_map}
    return {k: v / total for k, v in scaled.items()}


def run_simulation(entries: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    if not entries:
        raise ValueError("No drivers available for simulation. Run update_ratings first.")

    seed = int(safe_float(config.get("seed"), 20260303))
    simulations = max(int(safe_float(config.get("simulations"), MIN_SIMULATIONS)), MIN_SIMULATIONS)
    safety_car_probability = clamp(safe_float(config.get("safety_car_probability"), 0.22), 0.0, 1.0)
    overtaking_difficulty = clamp(safe_float(config.get("overtaking_difficulty"), 0.5), 0.0, 1.0)
    weather_modifier = clamp(safe_float(config.get("weather_modifier"), 0.0), -2.0, 2.0)
    track = config.get("track", {})
    if not isinstance(track, dict):
        track = {}
    tyre_degradation_factor = clamp(safe_float(track.get("tyre_degradation_factor"), 0.5), 0.0, 1.0)
    qualifying_noise = clamp(safe_float(track.get("qualifying_noise"), 2.6), 0.2, 8.0)
    race_noise = clamp(safe_float(track.get("race_noise"), 3.8), 0.5, 12.0)
    win_temperature = clamp(safe_float(config.get("win_temperature"), 1.0), 0.6, 1.8)

    rng = random.Random(seed)
    driver_names = [entry["name"] for entry in entries]
    finish_sum = {name: 0.0 for name in driver_names}
    win_count = {name: 0 for name in driver_names}
    podium_count = {name: 0 for name in driver_names}

    fixed_grid_config = config.get("fixed_grid")

    for _ in range(simulations):
        if isinstance(fixed_grid_config, list) and len(fixed_grid_config) > 0:
            grid = fixed_grid_config
        else:
            grid = simulate_qualifying(entries, rng, qualifying_noise=qualifying_noise)

        race_positions = simulate_single_race(
            entries=entries,
            grid_order=grid,
            rng=rng,
            safety_car_probability=safety_car_probability,
            overtaking_difficulty=overtaking_difficulty,
            weather_modifier=weather_modifier,
            tyre_degradation_factor=tyre_degradation_factor,
            race_noise=race_noise,
        )

        for name, finish in race_positions.items():
            finish_sum[name] += float(finish)
            if finish == 1:
                win_count[name] += 1
            if finish <= 3:
                podium_count[name] += 1

    raw_win_prob = {name: win_count[name] / simulations for name in driver_names}
    calibrated_win_prob = temperature_scale_distribution(raw_win_prob, temperature=win_temperature)

    rows: list[dict[str, Any]] = []
    for name in driver_names:
        rows.append(
            {
                "name": name,
                "win_probability": round(calibrated_win_prob[name], 6),
                "podium_probability": round(podium_count[name] / simulations, 6),
                "expected_finish": round(finish_sum[name] / simulations, 6),
            }
        )

    rows.sort(key=lambda x: (-x["win_probability"], x["expected_finish"], x["name"].lower()))

    return {
        "race": str(config.get("race") or "Next GP"),
        "generated_at": str(config.get("generated_at") or config.get("race_date") or "1970-01-01T00:00:00Z"),
        "deterministic_run_id": stable_hash_json(
            {
                "seed": seed,
                "simulations": simulations,
                "entries": entries,
                "config": config,
            }
        )[:20],
        "simulation": {
            "seed": seed,
            "simulations": simulations,
            "weather": str(config.get("weather") or "dry"),
            "safety_car_probability": round(safety_car_probability, 6),
            "overtaking_difficulty": round(overtaking_difficulty, 6),
            "win_temperature": round(win_temperature, 6),
        },
        "drivers": rows,
    }


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    required_paths = [
        Path(args.driver_ratings),
        Path(args.team_ratings),
        Path(args.strategy_scores),
        Path(args.reliability_scores),
    ]
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        if args.allow_missing_models:
            LOGGER.warning("Skipping simulation, missing model file(s): %s", ", ".join(missing))
            return 0
        LOGGER.error("simulate_race failed, missing model file(s): %s", ", ".join(missing))
        return 1

    try:
        driver_ratings = load_json(Path(args.driver_ratings))
        team_ratings = load_json(Path(args.team_ratings))
        strategy_scores = load_json(Path(args.strategy_scores))
        reliability_scores = load_json(Path(args.reliability_scores))
        race_config = load_or_default_config(Path(args.race_config))

        # CLI --fixed-grid overrides config["fixed_grid"]
        if args.fixed_grid:
            race_config["fixed_grid"] = [x.strip().upper() for x in args.fixed_grid.split(",") if x.strip()]

        entries = build_entries(driver_ratings, team_ratings, strategy_scores, reliability_scores)
        prediction = run_simulation(entries, race_config)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(prediction, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.error("simulate_race failed: %s", exc)
        return 1

    LOGGER.info("Wrote prediction output: %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
