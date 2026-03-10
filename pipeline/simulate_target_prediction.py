#!/usr/bin/env python3
"""
Target-aware prediction execution for qualifying, sprint qualifying, sprint and race.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.prediction_targeting import compute_weekend_form, find_event, load_json
from pipeline.simulate_race import (
    MIN_SIMULATIONS,
    build_entries,
    clamp,
    safe_float,
    simulate_qualifying,
    simulate_single_race,
    stable_hash_json,
    temperature_scale_distribution,
)


def apply_weekend_adjustments(entries: list[dict[str, Any]], config: dict[str, Any], raw_dir: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    season = int(safe_float(config.get("season"), 0))
    race_name = str(config.get("race") or "").strip()
    if season <= 0 or not race_name:
        return entries, {}

    snapshot_path = raw_dir / f"season_{season}.json"
    if not snapshot_path.exists():
        return entries, {}

    try:
        snapshot = load_json(snapshot_path)
        if not isinstance(snapshot, dict):
            return entries, {}
        event = find_event(snapshot, race_name)
        if event is None:
            return entries, {}
    except Exception:
        return entries, {}

    manifest = config.get("inputs_used")
    if not isinstance(manifest, list):
        manifest = []

    adjusted: list[dict[str, Any]] = []
    form_by_driver: dict[str, dict[str, Any]] = {}
    for entry in entries:
        form = compute_weekend_form(entry["name"], event, manifest)
        delta = float(form.get("delta") or 0.0)
        updated = dict(entry)
        updated["driver_rating"] = round(clamp(entry["driver_rating"] + 0.7 * delta, 1.0, 99.0), 6)
        updated["team_rating"] = round(clamp(entry["team_rating"] + 0.3 * delta, 1.0, 99.0), 6)
        updated["weekend_form_delta"] = round(delta, 6)
        adjusted.append(updated)
        form_by_driver[entry["name"]] = form
    return adjusted, form_by_driver


def _shared_prediction_meta(
    entries: list[dict[str, Any]],
    config: dict[str, Any],
    driver_ratings: dict[str, Any],
    team_ratings: dict[str, Any],
    form_by_driver: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    seed = int(safe_float(config.get("seed"), 20260303))
    simulations = max(int(safe_float(config.get("simulations"), MIN_SIMULATIONS)), MIN_SIMULATIONS)
    return {
        "race": str(config.get("race") or "Next GP"),
        "generated_at": str(config.get("generated_at") or config.get("race_date") or "1970-01-01T00:00:00Z"),
        "prediction_target": str(config.get("prediction_target") or "race"),
        "prediction_target_label": str(config.get("prediction_target_label") or "Race"),
        "target_session_code": str(config.get("target_session_code") or "R"),
        "target_output_type": str(config.get("target_output_type") or "race"),
        "weekend_format": str(config.get("weekend_format") or "standard"),
        "inputs_used": config.get("inputs_used", []),
        "deterministic_run_id": stable_hash_json(
            {
                "seed": seed,
                "simulations": simulations,
                "entries": entries,
                "config": config,
                "weekend_form": form_by_driver,
            }
        )[:20],
        "integrity": {
            "driver_ratings_hash": stable_hash_json(driver_ratings)[:12],
            "team_ratings_hash": stable_hash_json(team_ratings)[:12],
            "race_config_hash": stable_hash_json(config)[:12],
        },
    }


def run_qualifying_prediction(
    entries: list[dict[str, Any]],
    config: dict[str, Any],
    driver_ratings: dict[str, Any],
    team_ratings: dict[str, Any],
    form_by_driver: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not entries:
        raise ValueError("No drivers available for qualifying prediction.")

    import random

    seed = int(safe_float(config.get("seed"), 20260303))
    simulations = max(int(safe_float(config.get("simulations"), MIN_SIMULATIONS)), MIN_SIMULATIONS)
    track = config.get("track", {})
    if not isinstance(track, dict):
        track = {}
    qualifying_noise = clamp(safe_float(track.get("qualifying_noise"), 2.6), 0.2, 8.0)
    weather_modifier = clamp(safe_float(config.get("weather_modifier"), 0.0), -2.0, 2.0)
    qualifying_temperature = clamp(safe_float(config.get("qualifying_temperature"), 1.0), 0.6, 1.8)

    rng = random.Random(seed)
    names = [entry["name"] for entry in entries]
    pole_count = {name: 0 for name in names}
    front_row_count = {name: 0 for name in names}
    top10_count = {name: 0 for name in names}
    pos_sum = {name: 0.0 for name in names}
    top10_cutoff = min(10, len(entries))

    adjusted_noise = qualifying_noise + abs(weather_modifier) * 0.6

    for _ in range(simulations):
        grid = simulate_qualifying(entries, rng, qualifying_noise=adjusted_noise)
        for idx, name in enumerate(grid, start=1):
            pos_sum[name] += float(idx)
            if idx == 1:
                pole_count[name] += 1
            if idx <= 2:
                front_row_count[name] += 1
            if idx <= top10_cutoff:
                top10_count[name] += 1

    raw_pole_prob = {name: pole_count[name] / simulations for name in names}
    scaled_pole_prob = temperature_scale_distribution(raw_pole_prob, temperature=qualifying_temperature)

    driver_stats = {e["name"]: (e["team"], e["driver_rating"], e["team_rating"], e.get("weekend_form_delta", 0.0)) for e in entries}
    rows: list[dict[str, Any]] = []
    for name in names:
        team, d_rating, t_rating, form_delta = driver_stats.get(name, ("Unknown", 50.0, 50.0, 0.0))
        total = d_rating + t_rating
        d_share = (d_rating / total) * 100 if total > 0 else 50.0
        t_share = (t_rating / total) * 100 if total > 0 else 50.0
        rows.append(
            {
                "name": name,
                "team": team,
                "driver_rating": round(d_rating, 1),
                "team_rating": round(t_rating, 1),
                "driver_share": round(d_share, 1),
                "team_share": round(t_share, 1),
                "weekend_form_delta": round(form_delta, 6),
                "pole_probability": round(scaled_pole_prob[name], 6),
                "front_row_probability": round(front_row_count[name] / simulations, 6),
                "top10_probability": round(top10_count[name] / simulations, 6),
                "expected_position": round(pos_sum[name] / simulations, 6),
            }
        )
    rows.sort(key=lambda x: (-x["pole_probability"], x["expected_position"], x["name"].lower()))

    payload = _shared_prediction_meta(entries, config, driver_ratings, team_ratings, form_by_driver)
    payload["simulation"] = {
        "seed": seed,
        "simulations": simulations,
        "weather": str(config.get("weather") or "dry"),
        "grid_source": str(config.get("grid_source") or "simulation"),
        "available_sessions": config.get("available_sessions", []),
        "qualifying_noise": round(adjusted_noise, 6),
    }
    payload["drivers"] = rows
    return payload


def run_race_or_sprint_prediction(
    entries: list[dict[str, Any]],
    config: dict[str, Any],
    driver_ratings: dict[str, Any],
    team_ratings: dict[str, Any],
    form_by_driver: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    import random

    if not entries:
        raise ValueError("No drivers available for race prediction.")

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
        if isinstance(fixed_grid_config, list) and fixed_grid_config:
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

    driver_stats = {e["name"]: (e["team"], e["driver_rating"], e["team_rating"], e.get("weekend_form_delta", 0.0)) for e in entries}
    rows: list[dict[str, Any]] = []
    for name in driver_names:
        team, d_rating, t_rating, form_delta = driver_stats.get(name, ("Unknown", 50.0, 50.0, 0.0))
        total = d_rating + t_rating
        d_share = (d_rating / total) * 100 if total > 0 else 50.0
        t_share = (t_rating / total) * 100 if total > 0 else 50.0
        rows.append(
            {
                "name": name,
                "team": team,
                "driver_rating": round(d_rating, 1),
                "team_rating": round(t_rating, 1),
                "driver_share": round(d_share, 1),
                "team_share": round(t_share, 1),
                "weekend_form_delta": round(form_delta, 6),
                "win_probability": round(calibrated_win_prob[name], 6),
                "podium_probability": round(podium_count[name] / simulations, 6),
                "expected_finish": round(finish_sum[name] / simulations, 6),
            }
        )
    rows.sort(key=lambda x: (-x["win_probability"], x["expected_finish"], x["name"].lower()))

    payload = _shared_prediction_meta(entries, config, driver_ratings, team_ratings, form_by_driver)
    payload["simulation"] = {
        "seed": seed,
        "simulations": simulations,
        "weather": str(config.get("weather") or "dry"),
        "grid_source": str(config.get("grid_source") or "simulation"),
        "available_sessions": config.get("available_sessions", []),
        "safety_car_probability": round(safety_car_probability, 6),
        "overtaking_difficulty": round(overtaking_difficulty, 6),
        "win_temperature": round(win_temperature, 6),
    }
    payload["drivers"] = rows
    return payload


def run_target_prediction(
    driver_ratings: dict[str, Any],
    team_ratings: dict[str, Any],
    strategy_scores: dict[str, Any],
    reliability_scores: dict[str, Any],
    config: dict[str, Any],
    raw_dir: Path,
) -> dict[str, Any]:
    entries = build_entries(driver_ratings, team_ratings, strategy_scores, reliability_scores)
    if not entries:
        raise ValueError("No drivers available for prediction. Run update_ratings first.")

    adjusted_entries, form_by_driver = apply_weekend_adjustments(entries, config, raw_dir=raw_dir)
    target = str(config.get("prediction_target") or "race")

    if target in {"qualifying", "sprint_qualifying"}:
        return run_qualifying_prediction(adjusted_entries, config, driver_ratings, team_ratings, form_by_driver)
    return run_race_or_sprint_prediction(adjusted_entries, config, driver_ratings, team_ratings, form_by_driver)
