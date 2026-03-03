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
FILENAME_RE = re.compile(r"^signals_\d{4}-\d{2}-\d{2}\.json$")
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


def validate_signal(signal: dict[str, Any], file_label: str, idx: int) -> list[str]:
    prefix = f"{file_label} signal#{idx}"
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
            LOGGER.warning("Non-standard signal filename: %s (expected signals_YYYY-MM-DD.json)", path.name)

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
