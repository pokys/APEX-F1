#!/usr/bin/env python3
"""
Backtest simulation output quality against historical race outcomes.

This is a deterministic evaluation utility. It does not alter ratings.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

# Support running as script: `python pipeline/backtest_simulation.py`
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.select_next_gp import apply_track_profile, load_track_profiles
from pipeline.simulate_race import build_entries, load_json, load_or_default_config, run_simulation


LOGGER = logging.getLogger("backtest_simulation")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest simulation quality on historical races.")
    parser.add_argument("--season", type=int, required=True, help="Season to backtest.")
    parser.add_argument("--raw-dir", default="data/raw/fastf1", help="Directory with FastF1 raw snapshots.")
    parser.add_argument("--driver-ratings", default="models/driver_ratings.json", help="Driver ratings path.")
    parser.add_argument("--team-ratings", default="models/team_ratings.json", help="Team ratings path.")
    parser.add_argument("--strategy-scores", default="models/strategy_scores.json", help="Strategy scores path.")
    parser.add_argument("--reliability-scores", default="models/reliability_scores.json", help="Reliability scores path.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Base race config path.")
    parser.add_argument("--profiles", default="config/track_profiles.json", help="Track profile config path.")
    parser.add_argument("--simulations", type=int, default=2000, help="Simulations per historical race (default: 2000).")
    parser.add_argument("--output", default=None, help="Backtest output path. Default: outputs/backtest/backtest_season_<season>.json")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def normalize_position(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def find_race_results(event: dict[str, Any]) -> list[dict[str, Any]]:
    sessions = event.get("sessions")
    if not isinstance(sessions, list):
        return []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        if str(session.get("session_code") or "").upper() != "R":
            continue
        results = session.get("results")
        if isinstance(results, list):
            return [r for r in results if isinstance(r, dict)]
    return []


def build_event_config(
    base_config: dict[str, Any],
    profiles: dict[str, Any],
    season: int,
    round_number: int,
    event_name: str,
    country: str,
    event_date: str,
    simulations: int,
) -> dict[str, Any]:
    cfg = json.loads(json.dumps(base_config))
    cfg["season"] = season
    cfg["next_round"] = round_number
    cfg["race"] = event_name
    cfg["race_date"] = event_date
    cfg["generated_at"] = f"{event_date}T00:00:00Z"
    cfg["seed"] = int(f"{season}{round_number:02d}")
    cfg["simulations"] = simulations
    # Backtest recommendation is learned from raw simulation output.
    cfg["win_temperature"] = 1.0
    profile_key = apply_track_profile(cfg, {"event_name": event_name, "country": country}, profiles)
    if profile_key:
        cfg["track_profile"] = profile_key
    return cfg


def score_brier(prob_map: dict[str, float], actual_set: set[str]) -> float:
    keys = sorted(prob_map.keys())
    if not keys:
        return 0.0
    mse = 0.0
    for key in keys:
        p = prob_map[key]
        o = 1.0 if key in actual_set else 0.0
        mse += (p - o) ** 2
    return mse / len(keys)


def transform_distribution(prob_map: dict[str, float], temperature: float) -> dict[str, float]:
    eps = 1e-12
    power = 1.0 / max(temperature, eps)
    scaled = {k: max(v, eps) ** power for k, v in prob_map.items()}
    total = sum(scaled.values())
    if total <= 0:
        uniform = 1.0 / max(len(prob_map), 1)
        return {k: uniform for k in prob_map}
    return {k: v / total for k, v in scaled.items()}


def expected_calibration_error(conf_outcomes: list[tuple[float, int]], bins: int = 10) -> float:
    if not conf_outcomes:
        return 0.0
    bucket_totals = [0 for _ in range(bins)]
    bucket_conf = [0.0 for _ in range(bins)]
    bucket_acc = [0.0 for _ in range(bins)]
    for conf, outcome in conf_outcomes:
        idx = min(int(conf * bins), bins - 1)
        bucket_totals[idx] += 1
        bucket_conf[idx] += conf
        bucket_acc[idx] += float(outcome)

    n = len(conf_outcomes)
    ece = 0.0
    for i in range(bins):
        if bucket_totals[i] == 0:
            continue
        avg_conf = bucket_conf[i] / bucket_totals[i]
        avg_acc = bucket_acc[i] / bucket_totals[i]
        ece += (bucket_totals[i] / n) * abs(avg_conf - avg_acc)
    return ece


def optimize_win_temperature(per_race: list[dict[str, Any]]) -> dict[str, float]:
    if not per_race:
        return {"recommended_win_temperature": 1.0, "avg_log_loss": 0.0}

    best_temp = 1.0
    best_loss = float("inf")
    candidate_temps = [round(0.6 + 0.05 * i, 2) for i in range(21)]
    for temp in candidate_temps:
        total = 0.0
        count = 0
        for row in per_race:
            win_probs = row["win_probabilities"]
            actual = row["actual_winner"]
            dist = transform_distribution(win_probs, temperature=temp)
            total += -math.log(max(dist.get(actual, 0.0), 1e-12))
            count += 1
        avg = total / max(count, 1)
        if avg < best_loss:
            best_loss = avg
            best_temp = temp
    return {
        "recommended_win_temperature": best_temp,
        "avg_log_loss": round(best_loss, 6),
    }


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    season = args.season
    raw_path = Path(args.raw_dir) / f"season_{season}.json"
    if not raw_path.exists():
        LOGGER.error("Backtest failed: missing raw snapshot %s", raw_path)
        return 1

    output_path = Path(args.output) if args.output else Path(f"outputs/backtest/backtest_season_{season}.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        raw = load_json(raw_path)
        driver_ratings = load_json(Path(args.driver_ratings))
        team_ratings = load_json(Path(args.team_ratings))
        strategy_scores = load_json(Path(args.strategy_scores))
        reliability_scores = load_json(Path(args.reliability_scores))
        base_config = load_or_default_config(Path(args.race_config))
        profiles = load_track_profiles(Path(args.profiles))
    except Exception as exc:
        LOGGER.error("Backtest failed while loading inputs: %s", exc)
        return 1

    entries = build_entries(driver_ratings, team_ratings, strategy_scores, reliability_scores)
    entry_names = {row["name"] for row in entries}

    events = raw.get("events")
    if not isinstance(events, list):
        LOGGER.error("Backtest failed: invalid raw events payload.")
        return 1

    per_race: list[dict[str, Any]] = []
    winner_conf_outcomes: list[tuple[float, int]] = []
    for event in sorted((e for e in events if isinstance(e, dict)), key=lambda x: (x.get("round", 999), str(x.get("event_date") or ""))):
        round_number = normalize_position(event.get("round"))
        if round_number is None:
            continue
        results = find_race_results(event)
        if not results:
            continue

        winner = None
        podium: set[str] = set()
        for row in results:
            abbr = str(row.get("abbreviation") or "").strip()
            pos = normalize_position(row.get("position"))
            if not abbr or pos is None:
                continue
            if pos == 1:
                winner = abbr
            if pos <= 3:
                podium.add(abbr)
        if not winner or winner not in entry_names:
            continue

        event_name = str(event.get("event_name") or f"Round {round_number}")
        country = str(event.get("country") or "")
        event_date = str(event.get("event_date") or "1970-01-01")[:10]
        config = build_event_config(
            base_config=base_config,
            profiles=profiles,
            season=season,
            round_number=round_number,
            event_name=event_name,
            country=country,
            event_date=event_date,
            simulations=max(args.simulations, 500),
        )

        prediction = run_simulation(entries, config)
        win_prob = {row["name"]: float(row["win_probability"]) for row in prediction["drivers"]}
        podium_prob = {row["name"]: float(row["podium_probability"]) for row in prediction["drivers"]}

        predicted_winner = max(win_prob, key=win_prob.get)
        predicted_podium = {x["name"] for x in sorted(prediction["drivers"], key=lambda r: (-float(r["podium_probability"]), r["name"]))[:3]}

        winner_conf_outcomes.append((win_prob[predicted_winner], 1 if predicted_winner == winner else 0))
        per_race.append(
            {
                "round": round_number,
                "race": event_name,
                "event_date": event_date,
                "actual_winner": winner,
                "predicted_winner": predicted_winner,
                "winner_hit": predicted_winner == winner,
                "podium_overlap": len(predicted_podium.intersection(podium)),
                "brier_win": round(score_brier(win_prob, {winner}), 6),
                "brier_podium": round(score_brier(podium_prob, podium), 6),
                "winner_log_loss": round(-math.log(max(win_prob.get(winner, 0.0), 1e-12)), 6),
                "win_probabilities": win_prob,
            }
        )

    if not per_race:
        LOGGER.warning("No completed race results available for backtest in season %s.", season)
        payload = {
            "season": season,
            "races_evaluated": 0,
            "summary": {},
            "races": [],
        }
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
        return 0

    win_hits = sum(1 for r in per_race if r["winner_hit"])
    brier_win_avg = sum(r["brier_win"] for r in per_race) / len(per_race)
    brier_podium_avg = sum(r["brier_podium"] for r in per_race) / len(per_race)
    log_loss_avg = sum(r["winner_log_loss"] for r in per_race) / len(per_race)
    podium_overlap_avg = sum(r["podium_overlap"] for r in per_race) / len(per_race)
    temp_result = optimize_win_temperature(per_race)
    ece = expected_calibration_error(winner_conf_outcomes, bins=10)

    payload = {
        "season": season,
        "races_evaluated": len(per_race),
        "summary": {
            "winner_accuracy": round(win_hits / len(per_race), 6),
            "mean_podium_overlap_top3": round(podium_overlap_avg, 6),
            "mean_brier_win": round(brier_win_avg, 6),
            "mean_brier_podium": round(brier_podium_avg, 6),
            "mean_winner_log_loss": round(log_loss_avg, 6),
            "win_ece_10_bins": round(ece, 6),
            **temp_result,
        },
        "races": per_race,
    }

    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    LOGGER.info("Wrote backtest report: %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
