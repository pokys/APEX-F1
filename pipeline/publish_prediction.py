#!/usr/bin/env python3
"""
Validate and canonicalize prediction output for publication.

Input:
- outputs/prediction.json (or custom input path)

Output:
- outputs/prediction.json (or custom output path)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("publish_prediction")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and publish prediction JSON.")
    parser.add_argument("--input", default="outputs/prediction.json", help="Input prediction JSON path.")
    parser.add_argument("--output", default="outputs/prediction.json", help="Output prediction JSON path.")
    parser.add_argument("--allow-missing-input", action="store_true", help="Exit 0 when input is missing.")
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


def redistribute_to_target(
    entries: list[dict[str, Any]],
    value_key: str,
    lower_key: str,
    target: float,
) -> None:
    """Rescale the slack above a per-driver lower bound so that ``value_key`` sums to ``target``.

    The pole/win headline metric is smoothed and temperature-scaled, which lifts the
    tail drivers off zero. The ``max(value, lower)`` monotonicity clamps then leak that
    floor into ``front_row``/``top10``/``podium`` and inflate their sums past the exact
    counts (2 front-row spots, N top-10 spots, 3 podium spots) the validator asserts.

    To keep the invariants intact we trim only the slack above each lower bound, which
    preserves ``value >= lower`` (monotonicity) and restores the exact target sum.
    """
    if not entries:
        return
    values = [max(safe_float(e.get(value_key), 0.0), safe_float(e.get(lower_key), 0.0)) for e in entries]
    lowers = [safe_float(e.get(lower_key), 0.0) for e in entries]
    total = sum(values)
    lower_sum = sum(lowers)
    slack_total = total - lower_sum
    if total <= target or slack_total <= 0:
        # Nothing to trim, or no slack to redistribute; just persist the floored values.
        for entry, value in zip(entries, values):
            entry[value_key] = round(value, 6)
        return
    keep = (target - lower_sum) / slack_total
    for entry, value, lower in zip(entries, values, lowers):
        entry[value_key] = round(lower + (value - lower) * keep, 6)


def normalize_prediction(payload: dict[str, Any]) -> dict[str, Any]:
    target_output_type = str(payload.get("target_output_type") or "race")
    drivers_raw = payload.get("drivers", [])
    if not isinstance(drivers_raw, list):
        raise ValueError("Prediction payload missing 'drivers' list.")

    normalized_drivers: list[dict[str, Any]] = []
    for row in drivers_raw:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue

        driver_entry = {
            "name": name,
            "team": str(row.get("team") or "Unknown"),
        }

        if target_output_type == "qualifying":
            pole = clamp(safe_float(row.get("pole_probability"), 0.0), 0.0, 1.0)
            front_row = clamp(safe_float(row.get("front_row_probability"), 0.0), 0.0, 1.0)
            top10 = clamp(safe_float(row.get("top10_probability"), 0.0), 0.0, 1.0)
            expected_position = max(1.0, safe_float(row.get("expected_position"), 99.0))
            front_row = max(front_row, pole)
            top10 = max(top10, front_row)
            driver_entry["pole_probability"] = round(pole, 6)
            driver_entry["front_row_probability"] = round(front_row, 6)
            driver_entry["top10_probability"] = round(top10, 6)
            driver_entry["expected_position"] = round(expected_position, 6)
        else:
            win = clamp(safe_float(row.get("win_probability"), 0.0), 0.0, 1.0)
            podium = clamp(safe_float(row.get("podium_probability"), 0.0), 0.0, 1.0)
            podium = max(podium, win)
            expected_finish = max(1.0, safe_float(row.get("expected_finish"), 99.0))
            driver_entry["win_probability"] = round(win, 6)
            driver_entry["podium_probability"] = round(podium, 6)
            driver_entry["expected_finish"] = round(expected_finish, 6)

        if "driver_share" in row: driver_entry["driver_share"] = row["driver_share"]
        if "team_share" in row: driver_entry["team_share"] = row["team_share"]
        if "weekend_form_delta" in row: driver_entry["weekend_form_delta"] = row["weekend_form_delta"]

        normalized_drivers.append(driver_entry)

    if target_output_type == "qualifying":
        # Restore the exact spot counts the validator requires after the monotonic clamps.
        top10_target = float(min(10, len(normalized_drivers)))
        redistribute_to_target(normalized_drivers, "front_row_probability", "pole_probability", 2.0)
        redistribute_to_target(normalized_drivers, "top10_probability", "front_row_probability", top10_target)
        normalized_drivers.sort(key=lambda x: (-x["pole_probability"], x["expected_position"], x["name"].lower()))
    else:
        redistribute_to_target(normalized_drivers, "podium_probability", "win_probability", 3.0)
        normalized_drivers.sort(key=lambda x: (-x["win_probability"], x["expected_finish"], x["name"].lower()))

    # SYSTEMIC FIX: Preserve integrity and simulation metadata
    out = {
        "race": str(payload.get("race") or "Next GP"),
        "generated_at": str(payload.get("generated_at") or "1970-01-01T00:00:00Z"),
        "prediction_target": str(payload.get("prediction_target") or "race"),
        "prediction_target_label": str(payload.get("prediction_target_label") or "Race"),
        "target_session_code": str(payload.get("target_session_code") or "R"),
        "target_output_type": target_output_type,
        "weekend_format": str(payload.get("weekend_format") or "standard"),
        "drivers": normalized_drivers,
    }
    if "inputs_used" in payload: out["inputs_used"] = payload["inputs_used"]
    if "inputs_status" in payload: out["inputs_status"] = payload["inputs_status"]
    if "season_blend" in payload: out["season_blend"] = payload["season_blend"]
    if "integrity" in payload: out["integrity"] = payload["integrity"]
    if "simulation" in payload: out["simulation"] = payload["simulation"]
    if "deterministic_run_id" in payload: out["deterministic_run_id"] = payload["deterministic_run_id"]
    if "data_freshness" in payload: out["data_freshness"] = payload["data_freshness"]
    
    return out


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    input_path = Path(args.input)
    if not input_path.exists():
        if args.allow_missing_input:
            LOGGER.warning("Skipping publish step, prediction input missing: %s", input_path)
            return 0
        LOGGER.error("publish_prediction failed, input missing: %s", input_path)
        return 1

    try:
        raw = load_json(input_path)
        if not isinstance(raw, dict):
            raise ValueError("Prediction input must be a JSON object.")
        normalized = normalize_prediction(raw)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(normalized, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.error("publish_prediction failed: %s", exc)
        return 1

    LOGGER.info("Published prediction to %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
