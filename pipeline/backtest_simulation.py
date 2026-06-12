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

from pipeline.build_features import DEFAULT_SIGNAL_GUARDRAILS, build_features, load_recency_config
from pipeline.prediction_targeting import build_inputs_manifest, load_session_weights
from pipeline.select_next_gp import apply_track_profile, load_track_profiles
from pipeline.simulate_race import load_json, load_or_default_config
from pipeline.simulate_target_prediction import run_target_prediction
from pipeline.update_ratings import (
    aggregate_optional_signal_indexes,
    blend_features,
    compute_driver_ratings,
    compute_reliability_scores,
    compute_strategy_scores,
    compute_team_ratings,
    current_season_blend_weight,
)


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
    parser.add_argument("--session-weights", default="config/session_weights.json", help="Session weight config path.")
    parser.add_argument("--recency-config", default="config/recency.json", help="Recency weighting config path.")
    parser.add_argument("--processed-dir", default="data/processed", help="Directory with processed feature files for previous-season fallback.")
    parser.add_argument("--min-training-races", type=int, default=1, help="Skip races until this many prior races are available.")
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


def event_chronology_key(event: dict[str, Any]) -> tuple[str, int, str]:
    event_date = str(event.get("event_date") or "").strip()[:10] or "9999-12-31"
    round_number = normalize_position(event.get("round"))
    return (
        event_date,
        round_number if round_number is not None else 9999,
        str(event.get("event_name") or ""),
    )


def session_results(event: dict[str, Any], code: str) -> list[dict[str, Any]]:
    sessions = event.get("sessions")
    if not isinstance(sessions, list):
        return []
    code_key = code.upper()
    for session in sessions:
        if not isinstance(session, dict):
            continue
        if str(session.get("session_code") or "").upper() != code_key:
            continue
        results = session.get("results")
        if isinstance(results, list):
            return [row for row in results if isinstance(row, dict)]
    return []


def find_race_results(event: dict[str, Any]) -> list[dict[str, Any]]:
    return session_results(event, "R")


def entry_list_for_event(event: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    for code in ("Q", "SQ", "R", "S", "FP3", "FP2", "FP1"):
        rows = session_results(event, code)
        mapping: dict[str, str] = {}
        for row in rows:
            driver = str(row.get("abbreviation") or "").strip().upper()
            team = str(row.get("team_name") or "").strip()
            if driver and team:
                mapping[driver] = team
        if mapping:
            return mapping, sorted(set(mapping.values()))
    return {}, []


def available_sessions_for_event(event: dict[str, Any]) -> list[str]:
    sessions = event.get("sessions")
    if not isinstance(sessions, list):
        return []
    out: list[str] = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        results = session.get("results")
        if not isinstance(results, list) or not results:
            continue
        code = str(session.get("session_code") or "").strip().upper()
        if code and code not in out:
            out.append(code)
    return out


def actual_winner_and_podium(event: dict[str, Any]) -> tuple[str | None, set[str]]:
    winner: str | None = None
    podium: set[str] = set()
    for row in find_race_results(event):
        abbr = str(row.get("abbreviation") or "").strip().upper()
        pos = normalize_position(row.get("position"))
        if not abbr or pos is None:
            continue
        if pos == 1:
            winner = abbr
        if pos <= 3:
            podium.add(abbr)
    return winner, podium


def actual_pole(event: dict[str, Any]) -> str | None:
    for row in session_results(event, "Q"):
        abbr = str(row.get("abbreviation") or "").strip().upper()
        pos = normalize_position(row.get("position"))
        if abbr and pos == 1:
            return abbr
    return None


def fixed_grid_from_event(event: dict[str, Any]) -> list[str] | None:
    rows = session_results(event, "Q")
    ranked: list[tuple[int, str]] = []
    for row in rows:
        pos = normalize_position(row.get("position"))
        abbr = str(row.get("abbreviation") or "").strip().upper()
        if pos is not None and abbr:
            ranked.append((pos, abbr))
    if not ranked:
        return None
    ranked.sort()
    return [abbr for _, abbr in ranked]


def count_prior_races(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if find_race_results(event))


def build_models_from_prior_events(
    raw: dict[str, Any],
    prior_events: list[dict[str, Any]],
    target_event: dict[str, Any],
    recency_config: dict[str, Any],
    previous_features: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    season = int(raw.get("season"))
    training_snapshot = dict(raw)
    training_snapshot["events"] = prior_events
    features = build_features(training_snapshot, [], DEFAULT_SIGNAL_GUARDRAILS, recency_config=recency_config)

    max_starts = 0.0
    for row in features.get("drivers", []):
        if not isinstance(row, dict):
            continue
        starts = row.get("race_effective_starts", row.get("starts", 0.0))
        try:
            max_starts = max(max_starts, float(starts or 0.0))
        except (TypeError, ValueError):
            pass
    if previous_features and 0 < max_starts < 5:
        features = blend_features(features, previous_features, current_season_blend_weight(max_starts))

    active_drivers, active_teams = entry_list_for_event(target_event)
    wet_by_team, safety_by_team, penalties_by_team = aggregate_optional_signal_indexes([], DEFAULT_SIGNAL_GUARDRAILS)

    metadata = {
        "season": season,
        "source_features": "walk_forward_in_memory",
        "source_summary": f"Walk-forward prior events: {len(prior_events)}",
    }
    drivers = {**metadata, **compute_driver_ratings(features, wet_by_team, active_drivers)}
    teams = {**metadata, **compute_team_ratings(features, active_teams)}
    strategy = {**metadata, **compute_strategy_scores(features, safety_by_team, active_teams)}
    reliability = {**metadata, **compute_reliability_scores(features, penalties_by_team, active_teams)}
    return drivers, teams, strategy, reliability, features


def event_seed(season: int, event_date: str, salt: int) -> int:
    compact_date = "".join(ch for ch in event_date[:10] if ch.isdigit())
    try:
        return int(f"{compact_date}{salt}")
    except ValueError:
        return int(f"{season}{salt:02d}")



def build_event_config(
    base_config: dict[str, Any],
    profiles: dict[str, Any],
    season: int,
    round_number: int,
    event_name: str,
    country: str,
    event_date: str,
    simulations: int,
    prediction_target: str,
    inputs_used: list[dict[str, Any]],
    available_sessions: list[str],
    fixed_grid: list[str] | None = None,
) -> dict[str, Any]:
    cfg = json.loads(json.dumps(base_config))
    cfg["season"] = season
    cfg["next_round"] = round_number
    cfg["race"] = event_name
    cfg["race_date"] = event_date
    cfg["generated_at"] = f"{event_date}T00:00:00Z"
    cfg["seed"] = event_seed(season, event_date, 1 if prediction_target == "qualifying" else 2)
    cfg["simulations"] = simulations
    cfg["available_sessions"] = available_sessions
    cfg["inputs_used"] = inputs_used
    cfg["prediction_target"] = prediction_target
    cfg["prediction_target_label"] = "Qualifying" if prediction_target == "qualifying" else "Race"
    cfg["target_session_code"] = "Q" if prediction_target == "qualifying" else "R"
    cfg["target_output_type"] = "qualifying" if prediction_target == "qualifying" else "race"
    cfg["weekend_format"] = "sprint" if any(code in available_sessions for code in ("SQ", "S")) else "standard"
    # Backtest recommendations are learned from raw simulation output.
    cfg["win_temperature"] = 1.0
    cfg["qualifying_temperature"] = 1.0
    if fixed_grid:
        cfg["fixed_grid"] = fixed_grid
        cfg["grid_source"] = "qualifying_results"
    else:
        cfg.pop("fixed_grid", None)
        cfg["grid_source"] = "simulation"
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


def optimize_temperature(
    rows: list[dict[str, Any]],
    probability_key: str,
    actual_key: str,
    output_key: str,
    loss_key: str,
) -> dict[str, float]:
    if not rows:
        return {output_key: 1.0, loss_key: 0.0}

    best_temp = 1.0
    best_loss = float("inf")
    candidate_temps = [round(0.6 + 0.05 * i, 2) for i in range(21)]
    for temp in candidate_temps:
        total = 0.0
        count = 0
        for row in rows:
            probs = row[probability_key]
            actual = row[actual_key]
            dist = transform_distribution(probs, temperature=temp)
            total += -math.log(max(dist.get(actual, 0.0), 1e-12))
            count += 1
        avg = total / max(count, 1)
        if avg < best_loss:
            best_loss = avg
            best_temp = temp
    return {
        output_key: best_temp,
        loss_key: round(best_loss, 6),
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
        base_config = load_or_default_config(Path(args.race_config))
        profiles = load_track_profiles(Path(args.profiles))
        session_weights = load_session_weights(Path(args.session_weights))
        recency_config = load_recency_config(Path(args.recency_config))
    except Exception as exc:
        LOGGER.error("Backtest failed while loading inputs: %s", exc)
        return 1

    events = raw.get("events")
    if not isinstance(events, list):
        LOGGER.error("Backtest failed: invalid raw events payload.")
        return 1

    previous_features = None
    previous_features_path = Path(args.processed_dir) / f"features_season_{season - 1}.json"
    if previous_features_path.exists():
        try:
            previous_features = load_json(previous_features_path)
        except Exception as exc:
            LOGGER.warning("Could not load previous-season features %s: %s", previous_features_path, exc)
            previous_features = None

    chronological_events = sorted((e for e in events if isinstance(e, dict)), key=event_chronology_key)
    per_race: list[dict[str, Any]] = []
    per_qualifying: list[dict[str, Any]] = []
    winner_conf_outcomes: list[tuple[float, int]] = []
    pole_conf_outcomes: list[tuple[float, int]] = []
    prior_events: list[dict[str, Any]] = []

    for event in chronological_events:
        round_number = normalize_position(event.get("round"))
        if round_number is None:
            continue
        results = find_race_results(event)
        if not results:
            continue
        if count_prior_races(prior_events) < max(0, args.min_training_races):
            prior_events.append(event)
            continue

        winner, podium = actual_winner_and_podium(event)
        pole = actual_pole(event)
        if not winner:
            prior_events.append(event)
            continue

        event_name = str(event.get("event_name") or f"Round {round_number}")
        country = str(event.get("country") or "")
        event_date = str(event.get("event_date") or "1970-01-01")[:10]
        driver_ratings, team_ratings, strategy_scores, reliability_scores, _features = build_models_from_prior_events(
            raw=raw,
            prior_events=prior_events,
            target_event=event,
            recency_config=recency_config,
            previous_features=previous_features if season > 1 else None,
        )

        available_sessions = available_sessions_for_event(event)
        qualifying_inputs = build_inputs_manifest(
            target="qualifying",
            available_sessions=[code for code in available_sessions if code in {"FP1", "FP2", "FP3"}],
            session_weights=session_weights,
            active_signal_count=0,
        )
        race_inputs = build_inputs_manifest(
            target="race",
            available_sessions=[code for code in available_sessions if code in {"FP2", "FP3", "Q"}],
            session_weights=session_weights,
            active_signal_count=0,
        )

        if pole:
            qualifying_config = build_event_config(
                base_config=base_config,
                profiles=profiles,
                season=season,
                round_number=round_number,
                event_name=event_name,
                country=country,
                event_date=event_date,
                simulations=max(args.simulations, 500),
                prediction_target="qualifying",
                inputs_used=qualifying_inputs,
                available_sessions=[code for code in available_sessions if code in {"FP1", "FP2", "FP3"}],
            )
            qualifying_prediction = run_target_prediction(
                driver_ratings,
                team_ratings,
                strategy_scores,
                reliability_scores,
                qualifying_config,
                raw_dir=Path(args.raw_dir),
            )
            pole_prob = {row["name"]: float(row["pole_probability"]) for row in qualifying_prediction["drivers"]}
            if pole in pole_prob:
                predicted_pole = max(pole_prob, key=pole_prob.get)
                pole_conf_outcomes.append((pole_prob[predicted_pole], 1 if predicted_pole == pole else 0))
                per_qualifying.append(
                    {
                        "round": round_number,
                        "race": event_name,
                        "event_date": event_date,
                        "actual_pole": pole,
                        "predicted_pole": predicted_pole,
                        "pole_hit": predicted_pole == pole,
                        "pole_log_loss": round(-math.log(max(pole_prob.get(pole, 0.0), 1e-12)), 6),
                        "pole_probabilities": pole_prob,
                    }
                )

        race_config = build_event_config(
            base_config=base_config,
            profiles=profiles,
            season=season,
            round_number=round_number,
            event_name=event_name,
            country=country,
            event_date=event_date,
            simulations=max(args.simulations, 500),
            prediction_target="race",
            inputs_used=race_inputs,
            available_sessions=[code for code in available_sessions if code in {"FP2", "FP3", "Q"}],
            fixed_grid=fixed_grid_from_event(event),
        )

        prediction = run_target_prediction(
            driver_ratings,
            team_ratings,
            strategy_scores,
            reliability_scores,
            race_config,
            raw_dir=Path(args.raw_dir),
        )
        win_prob = {row["name"]: float(row["win_probability"]) for row in prediction["drivers"]}
        podium_prob = {row["name"]: float(row["podium_probability"]) for row in prediction["drivers"]}
        if winner not in win_prob:
            prior_events.append(event)
            continue

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
        prior_events.append(event)

    if not per_race:
        LOGGER.warning("No completed race results available for backtest in season %s.", season)
        payload = {
            "season": season,
            "races_evaluated": 0,
            "qualifying_sessions_evaluated": 0,
            "summary": {},
            "races": [],
            "qualifying": [],
        }
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
        return 0

    win_hits = sum(1 for r in per_race if r["winner_hit"])
    brier_win_avg = sum(r["brier_win"] for r in per_race) / len(per_race)
    brier_podium_avg = sum(r["brier_podium"] for r in per_race) / len(per_race)
    log_loss_avg = sum(r["winner_log_loss"] for r in per_race) / len(per_race)
    podium_overlap_avg = sum(r["podium_overlap"] for r in per_race) / len(per_race)
    temp_result = optimize_temperature(
        per_race,
        probability_key="win_probabilities",
        actual_key="actual_winner",
        output_key="recommended_win_temperature",
        loss_key="calibrated_win_log_loss",
    )
    quali_temp_result = optimize_temperature(
        per_qualifying,
        probability_key="pole_probabilities",
        actual_key="actual_pole",
        output_key="recommended_qualifying_temperature",
        loss_key="calibrated_pole_log_loss",
    )
    ece = expected_calibration_error(winner_conf_outcomes, bins=10)
    pole_ece = expected_calibration_error(pole_conf_outcomes, bins=10)
    pole_hits = sum(1 for row in per_qualifying if row["pole_hit"])
    pole_log_loss_avg = sum(row["pole_log_loss"] for row in per_qualifying) / max(len(per_qualifying), 1)

    payload = {
        "season": season,
        "races_evaluated": len(per_race),
        "qualifying_sessions_evaluated": len(per_qualifying),
        "backtest_mode": "walk_forward",
        "min_training_races": max(0, args.min_training_races),
        "summary": {
            "winner_accuracy": round(win_hits / len(per_race), 6),
            "mean_podium_overlap_top3": round(podium_overlap_avg, 6),
            "mean_brier_win": round(brier_win_avg, 6),
            "mean_brier_podium": round(brier_podium_avg, 6),
            "mean_winner_log_loss": round(log_loss_avg, 6),
            "pole_accuracy": round(pole_hits / max(len(per_qualifying), 1), 6),
            "mean_pole_log_loss": round(pole_log_loss_avg, 6),
            "win_ece_10_bins": round(ece, 6),
            "pole_ece_10_bins": round(pole_ece, 6),
            **temp_result,
            **quali_temp_result,
        },
        "races": per_race,
        "qualifying": per_qualifying,
    }

    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    LOGGER.info("Wrote backtest report: %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
