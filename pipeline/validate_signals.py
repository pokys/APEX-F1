#!/usr/bin/env python3
"""
Validate processed signal JSON files before they enter the model pipeline.

Accepted top-level formats per file:
- list[object]
- object with key "signals": list[object]
- single signal object
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("validate_signals")
FILENAME_RE = re.compile(r"^(signals_\d{4}-\d{2}-\d{2}|penalties_\d{4}(_auto)?)\.json$")
EVENT_SIGNAL_TYPES = {"grid_penalty", "pu_element_change", "race_ban", "driver_substitution"}
PU_ELEMENTS = {"ICE", "TC", "MGU-H", "MGU-K", "ES", "CE", "EX", "GEARBOX"}
DRIVER_CODE_RE = re.compile(r"^[A-Z]{3}$")
UPGRADE_MAGNITUDES = {"minor", "medium", "major"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate knowledge/processed signal JSON files.")
    parser.add_argument("--signals-dir", default="knowledge/processed", help="Directory containing signals JSON files.")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit 0 if no signal files are present.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def normalize_signals(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        payload = raw.get("signals")
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        return [raw]
    return []


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def ensure_range(errors: list[str], label: str, value: Any, lo: float, hi: float, prefix: str) -> None:
    if not is_number(value):
        errors.append(f"{prefix}: '{label}' must be numeric.")
        return
    v = float(value)
    if v < lo or v > hi:
        errors.append(f"{prefix}: '{label}' must be in range [{lo}, {hi}].")


def validate_event_signal(signal: dict[str, Any], prefix: str) -> list[str]:
    """Typed event signal: a fact about one GP (grid penalty, PU element
    change, race ban, driver substitution)."""
    errors: list[str] = []
    kind = str(signal.get("type") or "").strip().lower()
    source_name = signal.get("source_name")
    if not isinstance(source_name, str) or not source_name.strip():
        errors.append(f"{prefix}: 'source_name' is required and must be non-empty string.")
    source_url = signal.get("source_url")
    if not isinstance(source_url, str) or not source_url.startswith(("http://", "https://")):
        errors.append(f"{prefix}: 'source_url' must be an http(s) URL.")
    if not isinstance(signal.get("season"), int) or isinstance(signal.get("season"), bool):
        errors.append(f"{prefix}: 'season' must be an integer.")
    if not isinstance(signal.get("event"), str) or not signal["event"].strip():
        errors.append(f"{prefix}: 'event' (GP name as in the calendar) is required.")
    timestamp = signal.get("timestamp")
    if not isinstance(timestamp, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T", timestamp):
        errors.append(f"{prefix}: 'timestamp' must be an ISO datetime string.")
    if signal.get("source_confidence") is not None:
        ensure_range(errors, "source_confidence", signal["source_confidence"], 0.0, 1.0, prefix)
    applies_to = signal.get("applies_to", "race")
    if applies_to not in {"race", "sprint"}:
        errors.append(f"{prefix}: 'applies_to' must be 'race' or 'sprint'.")

    def check_driver(field: str) -> None:
        value = signal.get(field)
        if not isinstance(value, str) or not DRIVER_CODE_RE.match(value.strip().upper()):
            errors.append(f"{prefix}: '{field}' must be a three-letter driver code.")

    has_places = signal.get("places") is not None
    if has_places and (not isinstance(signal.get("places"), int) or isinstance(signal.get("places"), bool) or not 1 <= signal["places"] <= 60):
        errors.append(f"{prefix}: 'places' must be an integer in [1, 60].")
    for flag in ("back_of_grid", "pit_lane"):
        if flag in signal and not isinstance(signal[flag], bool):
            errors.append(f"{prefix}: '{flag}' must be boolean.")
    sanctions = int(has_places) + int(signal.get("back_of_grid") is True) + int(signal.get("pit_lane") is True)

    if kind == "grid_penalty":
        check_driver("driver")
        if sanctions != 1:
            errors.append(f"{prefix}: grid_penalty needs exactly one of 'places', 'back_of_grid'=true, 'pit_lane'=true.")
    elif kind == "pu_element_change":
        if not signal.get("driver") and not signal.get("team"):
            errors.append(f"{prefix}: pu_element_change needs 'driver' or 'team'.")
        if signal.get("driver"):
            check_driver("driver")
        elements = signal.get("elements", [])
        if not isinstance(elements, list) or any(str(e).upper() not in PU_ELEMENTS for e in elements):
            errors.append(f"{prefix}: 'elements' must be a list of {sorted(PU_ELEMENTS)}.")
        if sanctions > 1:
            errors.append(f"{prefix}: at most one of 'places', 'back_of_grid', 'pit_lane'.")
    elif kind == "race_ban":
        check_driver("driver")
    elif kind == "driver_substitution":
        check_driver("driver_out")
        check_driver("driver_in")
        if not isinstance(signal.get("team"), str) or not signal["team"].strip():
            errors.append(f"{prefix}: driver_substitution needs 'team'.")
    return errors


def validate_signal(signal: dict[str, Any], file_label: str, idx: int) -> list[str]:
    prefix = f"{file_label} signal#{idx}"
    if "type" in signal:
        if str(signal.get("type") or "").strip().lower() not in EVENT_SIGNAL_TYPES:
            return [f"{prefix}: 'type' must be one of {sorted(EVENT_SIGNAL_TYPES)}."]
        return validate_event_signal(signal, prefix)
    errors: list[str] = []

    source_name = signal.get("source_name")
    if not isinstance(source_name, str) or not source_name.strip():
        errors.append(f"{prefix}: 'source_name' is required and must be non-empty string.")

    article_hash = signal.get("article_hash")
    if not isinstance(article_hash, str) or not article_hash.strip():
        errors.append(f"{prefix}: 'article_hash' is required and must be non-empty string.")

    source_confidence = signal.get("source_confidence")
    if source_confidence is None:
        errors.append(f"{prefix}: 'source_confidence' is required.")
    else:
        ensure_range(errors, "source_confidence", source_confidence, 0.0, 1.0, prefix)

    team = signal.get("team")
    driver = signal.get("driver") or signal.get("driver_name")
    if (not isinstance(team, str) or not team.strip()) and (not isinstance(driver, str) or not driver.strip()):
        errors.append(f"{prefix}: at least one of 'team' or 'driver'/'driver_name' must be present.")

    if "upgrade_detected" in signal and not isinstance(signal["upgrade_detected"], bool):
        errors.append(f"{prefix}: 'upgrade_detected' must be boolean when present.")

    if signal.get("upgrade_detected") is True:
        magnitude = signal.get("upgrade_magnitude")
        if not isinstance(magnitude, str) or magnitude.strip().lower() not in UPGRADE_MAGNITUDES:
            errors.append(f"{prefix}: 'upgrade_magnitude' must be one of {sorted(UPGRADE_MAGNITUDES)} when upgrade_detected=true.")
        component = signal.get("upgrade_component")
        if component is not None and (not isinstance(component, str) or not component.strip()):
            errors.append(f"{prefix}: 'upgrade_component' must be non-empty string when present.")

    if "reliability_concern" in signal and signal["reliability_concern"] is not None:
        ensure_range(errors, "reliability_concern", signal["reliability_concern"], 0.0, 1.0, prefix)

    if "driver_confidence_change" in signal and signal["driver_confidence_change"] is not None:
        ensure_range(errors, "driver_confidence_change", signal["driver_confidence_change"], -1.0, 1.0, prefix)

    extraction_version = signal.get("extraction_version")
    if extraction_version is not None and (not isinstance(extraction_version, str) or not extraction_version.strip()):
        errors.append(f"{prefix}: 'extraction_version' must be non-empty string when present.")

    return errors


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    signals_dir = Path(args.signals_dir)
    if not signals_dir.exists():
        if args.allow_empty:
            LOGGER.warning("Signals directory does not exist: %s", signals_dir)
            return 0
        LOGGER.error("Signals directory does not exist: %s", signals_dir)
        return 1

    files = sorted(signals_dir.glob("*.json"))
    if not files:
        if args.allow_empty:
            LOGGER.info("No signal files found in %s.", signals_dir)
            return 0
        LOGGER.error("No signal files found in %s.", signals_dir)
        return 1

    errors: list[str] = []
    total_signals = 0
    for path in files:
        if not FILENAME_RE.match(path.name):
            LOGGER.warning("Non-standard signal filename: %s (expected signals_YYYY-MM-DD.json or penalties_YYYY.json)", path.name)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{path}: invalid JSON ({exc})")
            continue

        signals = normalize_signals(raw)
        if not signals:
            errors.append(f"{path}: no valid signal objects found.")
            continue

        for idx, signal in enumerate(signals, start=1):
            total_signals += 1
            errors.extend(validate_signal(signal, str(path), idx))

    if errors:
        for line in errors:
            LOGGER.error(line)
        LOGGER.error("Signals validation failed with %d issue(s).", len(errors))
        return 1

    LOGGER.info("Signals validation passed for %d file(s), %d signal(s).", len(files), total_signals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
