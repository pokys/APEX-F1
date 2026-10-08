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
from typing import Any, Callable

# Support running as script: `python pipeline/backtest_simulation.py`
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.build_features import DEFAULT_SIGNAL_GUARDRAILS, build_features, load_recency_config
from pipeline.prediction_targeting import (
    build_inputs_manifest,
    championship_points_before,
    load_session_weights,
    session_has_classification,
    standings_weights,
)
from pipeline.select_next_gp import apply_track_profile, load_track_profiles, reset_track_params
from pipeline.simulate_race import load_json, load_or_default_config
from pipeline.simulate_target_prediction import run_target_prediction
from pipeline.update_ratings import blend_with_previous_season, build_rating_models


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
    parser.add_argument(
        "--search-simulations",
        type=int,
        default=1000,
        help="Simulations per event while searching the calibrated noise scale (default: 1000).",
    )
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
        if not session_has_classification(session.get("results")):
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
    """The actual starting grid (race grid positions, pit-lane starters
    last), so historical grid penalties are part of the backtest. Falls back
    to the qualifying order when grid positions are missing."""
    starting: list[tuple[int, str]] = []
    for row in find_race_results(event):
        grid = normalize_position(row.get("grid_position"))
        abbr = str(row.get("abbreviation") or "").strip().upper()
        status = str(row.get("status") or "").lower()
        if grid is None or not abbr or "did not start" in status:
            continue
        starting.append((grid if grid > 0 else 999, abbr))
    if starting:
        return [abbr for _, abbr in sorted(starting)]
    return qualifying_order_from_event(event)


def qualifying_order_from_event(event: dict[str, Any]) -> list[str] | None:
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
    """Walk-forward models: features from prior events only, then the same
    blending and rating code production uses (update_ratings)."""
    season = int(raw.get("season"))
    training_snapshot = dict(raw)
    training_snapshot["events"] = prior_events
    features = build_features(training_snapshot, [], DEFAULT_SIGNAL_GUARDRAILS, recency_config=recency_config)
    features, source_summary = blend_with_previous_season(features, previous_features)

    active_drivers, active_teams = entry_list_for_event(target_event)
    metadata = {
        "season": season,
        "source_features": "walk_forward_in_memory",
        "source_summary": f"Walk-forward prior events: {len(prior_events)} ({source_summary})",
    }
    drivers, teams, strategy, reliability = build_rating_models(
        features, active_drivers, active_teams, [], DEFAULT_SIGNAL_GUARDRAILS, metadata
    )
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
    # The base config is the live race_config of the upcoming GP; nothing
    # track- or calibration-specific may leak into historical events.
    reset_track_params(cfg)
    for key in ("win_temperature", "qualifying_temperature", "race_noise_scale", "qualifying_noise_scale", "fixed_grid", "grid_penalties"):
        cfg.pop(key, None)
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
    if fixed_grid:
        cfg["fixed_grid"] = fixed_grid
        cfg["grid_source"] = "actual_grid"
    else:
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


def mc_probabilities(frequencies: dict[str, float], simulations: int) -> dict[str, float]:
    """Monte Carlo frequencies with a half-count prior, so an outcome that
    never occurred in N simulations gets ~1/(2N) instead of exactly zero
    (which would make log loss infinite rather than just large)."""
    n = max(len(frequencies), 1)
    denom = simulations + 0.5 * n
    return {k: (v * simulations + 0.5) / denom for k, v in frequencies.items()}


def log_loss(prob_map: dict[str, float], actual: str) -> float:
    return -math.log(max(prob_map.get(actual, 0.0), 1e-12))


# ---------------------------------------------------------------- baselines

BASELINE_REPEAT_MASS = 0.3


def uniform_baseline(names: list[str]) -> dict[str, float]:
    return {name: 1.0 / len(names) for name in names} if names else {}


def repeat_baseline(names: list[str], previous: str | None) -> dict[str, float]:
    """The previous event's pole sitter/winner gets a fixed share, the rest
    is spread evenly."""
    if not names:
        return {}
    if previous not in names:
        return uniform_baseline(names)
    rest = (1.0 - BASELINE_REPEAT_MASS) / max(len(names) - 1, 1)
    return {name: (BASELINE_REPEAT_MASS if name == previous else rest) for name in names}


def championship_points(events: list[dict[str, Any]]) -> dict[str, float]:
    return championship_points_before({"events": events}, None)


def baseline_distributions(
    names: list[str], prior_events: list[dict[str, Any]], kind: str
) -> dict[str, dict[str, float]]:
    previous = None
    for event in reversed(prior_events):
        previous = actual_pole(event) if kind == "pole" else actual_winner_and_podium(event)[0]
        if previous:
            break
    return {
        "uniform": uniform_baseline(names),
        "previous_event": repeat_baseline(names, previous),
        "championship_order": standings_weights(names, championship_points(prior_events)),
    }


# ------------------------------------------------------------- calibration

INITIAL_NOISE_SCALES = (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0)
MAX_NOISE_SCALE = 24.0
MIN_NOISE_SCALE = 0.25


def search_noise_scale(loss_for_scale: Callable[[float], float], initial: tuple[float, ...] = INITIAL_NOISE_SCALES) -> tuple[float, float, dict[float, float]]:
    """Grid search over the simulation noise scale. When the best scale is on
    the edge of the grid, the grid is extended in that direction, so the
    optimum is never just the search boundary."""
    losses: dict[float, float] = {}
    grid = sorted(set(initial))
    while True:
        for scale in grid:
            if scale not in losses:
                losses[scale] = loss_for_scale(scale)
        ordered = sorted(losses)
        best = min(ordered, key=lambda s: (losses[s], s))
        if best == ordered[-1] and best < MAX_NOISE_SCALE:
            grid = [round(min(best * 1.5, MAX_NOISE_SCALE), 4)]
            continue
        if best == ordered[0] and best > MIN_NOISE_SCALE:
            grid = [round(max(best / 1.5, MIN_NOISE_SCALE), 4)]
            continue
        return best, losses[best], losses


BLEND_GRID = tuple(round(0.1 * i, 1) for i in range(10))
# Weight of the championship-order model in the published mixture. Fixed a
# priori (equal-weight ensemble) instead of tuned: on 2026 a tuned weight was
# driven by a single surprise pole and did not hold up in leave-one-out.
DEFAULT_STANDINGS_BLEND = 0.5


def mix(model: dict[str, float], baseline: dict[str, float], weight: float) -> dict[str, float]:
    names = set(model) | set(baseline)
    return {name: (1.0 - weight) * model.get(name, 0.0) + weight * baseline.get(name, 0.0) for name in names}


def blend_report(rows: list[tuple[dict[str, float], dict[str, float], str]], fixed: float = DEFAULT_STANDINGS_BLEND) -> dict[str, float]:
    """Loss of the fixed-weight mixture plus, for information, the weight a
    tuned search would pick and its leave-one-out loss."""
    if not rows:
        return {"weight": fixed, "loss": 0.0, "tuned_weight": fixed, "tuned_in_sample_loss": 0.0, "tuned_leave_one_out_loss": 0.0}
    tuned, tuned_loss, loo = choose_blend(rows)
    fixed_loss = mean([log_loss(mix(model, base, fixed), actual) for model, base, actual in rows])
    return {
        "weight": fixed,
        "loss": round(fixed_loss, 6),
        "tuned_weight": tuned,
        "tuned_in_sample_loss": round(tuned_loss, 6),
        "tuned_leave_one_out_loss": round(loo, 6),
    }


def choose_blend(rows: list[tuple[dict[str, float], dict[str, float], str]]) -> tuple[float, float, float]:
    """Weight a search over BLEND_GRID would pick: (in-sample best weight,
    its loss, leave-one-out loss where each event is scored with the weight
    chosen on the other events)."""
    if not rows:
        return 0.0, 0.0, 0.0

    def loss(weight: float, subset: list[tuple[dict[str, float], dict[str, float], str]]) -> float:
        return mean([log_loss(mix(model, base, weight), actual) for model, base, actual in subset])

    best = min(BLEND_GRID, key=lambda w: (loss(w, rows), w))
    loo: list[float] = []
    for idx, row in enumerate(rows):
        others = rows[:idx] + rows[idx + 1:]
        weight = min(BLEND_GRID, key=lambda w: (loss(w, others), w)) if others else best
        loo.append(log_loss(mix(row[0], row[1], weight), row[2]))
    return best, loss(best, rows), mean(loo)


# --------------------------------------------------------------- main loop


def collect_cases(args: argparse.Namespace, raw: dict[str, Any], season: int) -> list[dict[str, Any]]:
    base_config = load_or_default_config(Path(args.race_config))
    profiles = load_track_profiles(Path(args.profiles))
    session_weights = load_session_weights(Path(args.session_weights))
    recency_config = load_recency_config(Path(args.recency_config))

    previous_features = None
    previous_features_path = Path(args.processed_dir) / f"features_season_{season - 1}.json"
    if previous_features_path.exists():
        try:
            previous_features = load_json(previous_features_path)
        except Exception as exc:
            LOGGER.warning("Could not load previous-season features %s: %s", previous_features_path, exc)

    events = [e for e in raw.get("events") or [] if isinstance(e, dict)]
    cases: list[dict[str, Any]] = []
    prior_events: list[dict[str, Any]] = []
    for event in sorted(events, key=event_chronology_key):
        round_number = normalize_position(event.get("round"))
        if round_number is None or not find_race_results(event):
            continue
        winner, podium = actual_winner_and_podium(event)
        if count_prior_races(prior_events) < max(0, args.min_training_races) or not winner:
            prior_events.append(event)
            continue

        event_name = str(event.get("event_name") or f"Round {round_number}")
        country = str(event.get("country") or "")
        event_date = str(event.get("event_date") or "1970-01-01")[:10]
        models = build_models_from_prior_events(
            raw=raw,
            prior_events=prior_events,
            target_event=event,
            recency_config=recency_config,
            previous_features=previous_features,
        )[:4]
        available_sessions = available_sessions_for_event(event)
        practice = [code for code in available_sessions if code in {"FP1", "FP2", "FP3"}]
        race_sessions = [code for code in available_sessions if code in {"FP2", "FP3", "Q"}]
        common = dict(
            base_config=base_config,
            profiles=profiles,
            season=season,
            round_number=round_number,
            event_name=event_name,
            country=country,
            event_date=event_date,
            simulations=max(args.simulations, 500),
        )
        qualifying_config = build_event_config(
            **common,
            prediction_target="qualifying",
            inputs_used=build_inputs_manifest("qualifying", practice, session_weights, 0),
            available_sessions=practice,
        )
        race_config = build_event_config(
            **common,
            prediction_target="race",
            inputs_used=build_inputs_manifest("race", race_sessions, session_weights, 0),
            available_sessions=race_sessions,
            fixed_grid=fixed_grid_from_event(event),
        )
        entrants = sorted(entry_list_for_event(event)[0])
        cases.append(
            {
                "round": round_number,
                "race": event_name,
                "event_date": event_date,
                "models": models,
                "qualifying_config": qualifying_config,
                "race_config": race_config,
                "pole": actual_pole(event),
                "winner": winner,
                "podium": podium,
                "pole_baselines": baseline_distributions(entrants, prior_events, "pole"),
                "win_baselines": baseline_distributions(entrants, prior_events, "win"),
            }
        )
        prior_events.append(event)
    return cases


def predict_case(case: dict[str, Any], target: str, scale: float, simulations: int, raw_dir: Path) -> dict[str, Any]:
    config = json.loads(json.dumps(case["qualifying_config" if target == "qualifying" else "race_config"]))
    config["simulations"] = simulations
    config["qualifying_noise_scale"] = scale if target == "qualifying" else 1.0
    if target == "race":
        config["race_noise_scale"] = scale
    return run_target_prediction(*case["models"], config, raw_dir=raw_dir)


def pole_probabilities(prediction: dict[str, Any], simulations: int) -> dict[str, float]:
    return mc_probabilities({row["name"]: float(row["pole_probability"]) for row in prediction["drivers"]}, simulations)


def win_probabilities(prediction: dict[str, Any], simulations: int) -> dict[str, float]:
    return mc_probabilities({row["name"]: float(row["win_probability"]) for row in prediction["drivers"]}, simulations)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


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
    raw_dir = Path(args.raw_dir)

    try:
        raw = load_json(raw_path)
        cases = collect_cases(args, raw, season)
    except Exception as exc:
        LOGGER.error("Backtest failed while preparing events: %s", exc)
        return 1

    pole_cases = [case for case in cases if case["pole"]]
    if not cases:
        LOGGER.warning("No completed race results available for backtest in season %s.", season)
        payload = {"season": season, "races_evaluated": 0, "qualifying_sessions_evaluated": 0, "summary": {}, "races": [], "qualifying": []}
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
        return 0

    search_sims = max(500, min(args.simulations, args.search_simulations))

    def pole_loss(scale: float) -> float:
        losses = []
        for case in pole_cases:
            probs = pole_probabilities(predict_case(case, "qualifying", scale, search_sims, raw_dir), search_sims)
            losses.append(log_loss(probs, case["pole"]))
        return mean(losses)

    def win_loss(scale: float) -> float:
        losses = []
        for case in cases:
            probs = win_probabilities(predict_case(case, "race", scale, search_sims, raw_dir), search_sims)
            losses.append(log_loss(probs, case["winner"]))
        return mean(losses)

    qualifying_scale, _, qualifying_grid = search_noise_scale(pole_loss) if pole_cases else (1.0, 0.0, {})
    race_scale, _, race_grid = search_noise_scale(win_loss)
    LOGGER.info("Calibrated noise scales: qualifying=%s race=%s", qualifying_scale, race_scale)

    sims = max(args.simulations, 500)
    pole_rows: list[tuple[dict[str, Any], dict[str, float], dict[str, float]]] = []
    for case in pole_cases:
        raw_probs = pole_probabilities(predict_case(case, "qualifying", 1.0, sims, raw_dir), sims)
        probs = pole_probabilities(predict_case(case, "qualifying", qualifying_scale, sims, raw_dir), sims)
        if case["pole"] in probs:
            pole_rows.append((case, raw_probs, probs))
    pole_blend = blend_report(
        [(probs, case["pole_baselines"]["championship_order"], case["pole"]) for case, _, probs in pole_rows]
    )
    qualifying_blend = pole_blend["weight"]

    per_qualifying: list[dict[str, Any]] = []
    pole_conf_outcomes: list[tuple[float, int]] = []
    raw_pole_losses: list[float] = []
    for case, raw_probs, model_probs in pole_rows:
        probs = mix(model_probs, case["pole_baselines"]["championship_order"], qualifying_blend)
        predicted = max(probs, key=lambda k: (probs[k], k))
        pole_conf_outcomes.append((probs[predicted], 1 if predicted == case["pole"] else 0))
        raw_pole_losses.append(log_loss(raw_probs, case["pole"]))
        per_qualifying.append(
            {
                "round": case["round"],
                "race": case["race"],
                "event_date": case["event_date"],
                "actual_pole": case["pole"],
                "predicted_pole": predicted,
                "pole_hit": predicted == case["pole"],
                "pole_log_loss": round(log_loss(probs, case["pole"]), 6),
                "model_only_log_loss": round(log_loss(model_probs, case["pole"]), 6),
                "baseline_log_loss": {name: round(log_loss(dist, case["pole"]), 6) for name, dist in case["pole_baselines"].items()},
                "pole_probabilities": {k: round(v, 6) for k, v in sorted(probs.items())},
            }
        )

    race_rows: list[tuple[dict[str, Any], dict[str, float], dict[str, float], dict[str, Any]]] = []
    for case in cases:
        raw_probs = win_probabilities(predict_case(case, "race", 1.0, sims, raw_dir), sims)
        prediction = predict_case(case, "race", race_scale, sims, raw_dir)
        probs = win_probabilities(prediction, sims)
        if case["winner"] in probs:
            race_rows.append((case, raw_probs, probs, prediction))
    win_blend = blend_report(
        [(probs, case["win_baselines"]["championship_order"], case["winner"]) for case, _, probs, _ in race_rows]
    )
    race_blend = win_blend["weight"]

    per_race: list[dict[str, Any]] = []
    winner_conf_outcomes: list[tuple[float, int]] = []
    raw_win_losses: list[float] = []
    for case, raw_probs, model_probs, prediction in race_rows:
        probs = mix(model_probs, case["win_baselines"]["championship_order"], race_blend)
        podium_prob = {row["name"]: float(row["podium_probability"]) for row in prediction["drivers"]}
        predicted = max(probs, key=lambda k: (probs[k], k))
        predicted_podium = {row["name"] for row in sorted(prediction["drivers"], key=lambda r: (-float(r["podium_probability"]), r["name"]))[:3]}
        winner_conf_outcomes.append((probs[predicted], 1 if predicted == case["winner"] else 0))
        raw_win_losses.append(log_loss(raw_probs, case["winner"]))
        per_race.append(
            {
                "round": case["round"],
                "race": case["race"],
                "event_date": case["event_date"],
                "actual_winner": case["winner"],
                "predicted_winner": predicted,
                "winner_hit": predicted == case["winner"],
                "podium_overlap": len(predicted_podium.intersection(case["podium"])),
                "brier_win": round(score_brier(probs, {case["winner"]}), 6),
                "brier_podium": round(score_brier(podium_prob, case["podium"]), 6),
                "winner_log_loss": round(log_loss(probs, case["winner"]), 6),
                "model_only_log_loss": round(log_loss(model_probs, case["winner"]), 6),
                "baseline_log_loss": {name: round(log_loss(dist, case["winner"]), 6) for name, dist in case["win_baselines"].items()},
                "win_probabilities": {k: round(v, 6) for k, v in sorted(probs.items())},
            }
        )

    def baseline_means(rows: list[dict[str, Any]]) -> dict[str, float]:
        names = sorted({name for row in rows for name in row["baseline_log_loss"]})
        return {name: round(mean([row["baseline_log_loss"][name] for row in rows]), 6) for name in names}

    pole_baselines = baseline_means(per_qualifying)
    win_baselines = baseline_means(per_race)
    calibrated_pole = pole_blend["loss"]
    calibrated_win = win_blend["loss"]
    summary = {
        "winner_accuracy": round(mean([1.0 if r["winner_hit"] else 0.0 for r in per_race]), 6),
        "mean_podium_overlap_top3": round(mean([r["podium_overlap"] for r in per_race]), 6),
        "mean_brier_win": round(mean([r["brier_win"] for r in per_race]), 6),
        "mean_brier_podium": round(mean([r["brier_podium"] for r in per_race]), 6),
        "mean_winner_log_loss": round(mean(raw_win_losses), 6),
        "calibrated_win_log_loss": round(calibrated_win, 6),
        "pole_accuracy": round(mean([1.0 if r["pole_hit"] else 0.0 for r in per_qualifying]), 6),
        "mean_pole_log_loss": round(mean(raw_pole_losses), 6),
        "calibrated_pole_log_loss": round(calibrated_pole, 6),
        "win_ece_10_bins": round(expected_calibration_error(winner_conf_outcomes), 6),
        "pole_ece_10_bins": round(expected_calibration_error(pole_conf_outcomes), 6),
        "recommended_qualifying_noise_scale": qualifying_scale,
        "recommended_race_noise_scale": race_scale,
        "recommended_standings_blend_qualifying": qualifying_blend,
        "recommended_standings_blend_race": race_blend,
        "standings_blend_search": {"pole": pole_blend, "win": win_blend},
        "model_only_pole_log_loss": round(mean([row["model_only_log_loss"] for row in per_qualifying]), 6),
        "model_only_win_log_loss": round(mean([row["model_only_log_loss"] for row in per_race]), 6),
        "baseline_pole_log_loss": pole_baselines,
        "baseline_win_log_loss": win_baselines,
        "best_baseline_pole_log_loss": min(pole_baselines.values()) if pole_baselines else None,
        "best_baseline_win_log_loss": min(win_baselines.values()) if win_baselines else None,
        "noise_scale_search": {
            "qualifying": {str(k): round(v, 6) for k, v in sorted(qualifying_grid.items())},
            "race": {str(k): round(v, 6) for k, v in sorted(race_grid.items())},
            "simulations_per_event": search_sims,
        },
    }
    payload = {
        "season": season,
        "races_evaluated": len(per_race),
        "qualifying_sessions_evaluated": len(per_qualifying),
        "backtest_mode": "walk_forward",
        "min_training_races": max(0, args.min_training_races),
        "summary": summary,
        "races": per_race,
        "qualifying": per_qualifying,
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    LOGGER.info("Wrote backtest report: %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
