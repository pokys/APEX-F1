#!/usr/bin/env python3
"""
Collect official Pirelli weekend compounds into a versioned JSON file.

This is supplemental metadata for the web report only. It must never affect
simulation results directly.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import requests


LOGGER = logging.getLogger("collect_tyre_compounds")
USER_AGENT = "APEX-F1/1.0 (+https://github.com/pokys/APEX-F1)"
COMPOUND_RE = re.compile(r"(C[1-6])[^C]{0,40}(C[1-6])[^C]{0,40}(C[1-6])", re.IGNORECASE)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect official Pirelli tyre compounds for F1 weekends.")
    parser.add_argument("--season", type=int, required=True, help="Season year.")
    parser.add_argument("--calendar-cache-dir", default="data/raw/calendars", help="Normalized calendar cache directory.")
    parser.add_argument("--source-config", default="config/tyre_sources.json", help="Tyre source config JSON path.")
    parser.add_argument("--output-dir", default="data/raw/tyres", help="Output directory for tyre metadata.")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def slug(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


def load_sources(path: Path, season: int) -> list[str]:
    if not path.exists():
        return []
    raw = load_json(path)
    season_sources = raw.get(str(season), [])
    urls: list[str] = []
    for row in season_sources:
        if isinstance(row, str):
            urls.append(row)
        elif isinstance(row, dict) and isinstance(row.get("url"), str):
            urls.append(row["url"])
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def load_calendar(calendar_cache_dir: Path, season: int) -> list[dict[str, Any]]:
    path = calendar_cache_dir / f"season_{season}.json"
    if not path.exists():
        return []
    raw = load_json(path)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def fetch_html(url: str, timeout: int) -> str:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response.text


def html_to_text(raw_html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    return " ".join(text.split())


def extract_title(raw_html: str, fallback_url: str) -> str:
    match = TITLE_RE.search(raw_html)
    if not match:
        return fallback_url
    value = html.unescape(match.group(1))
    return " ".join(value.split())


def event_aliases(event: dict[str, Any]) -> list[str]:
    values = [
      str(event.get("event_name") or "").strip(),
      str(event.get("location") or "").strip(),
      str(event.get("country") or "").strip(),
    ]
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for candidate in {value, value.replace(" Grand Prix", "").strip()}:
            norm = candidate.lower()
            if norm and norm not in seen:
                seen.add(norm)
                aliases.append(candidate)
    return aliases


def extract_compounds_for_event(text: str, aliases: list[str]) -> dict[str, str] | None:
    lowered = text.lower()
    for alias in aliases:
        alias_lower = alias.lower()
        idx = lowered.find(alias_lower)
        if idx < 0:
            continue
        window = text[idx : idx + 400]
        match = COMPOUND_RE.search(window)
        if not match:
            continue
        compounds = [token.upper() for token in match.groups()]
        if len(set(compounds)) != 3:
            continue
        compounds.sort(key=lambda value: int(value[1:]))
        return {"hard": compounds[0], "medium": compounds[1], "soft": compounds[2]}
    return None


def merge_entries(existing: list[dict[str, Any]], updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[int, dict[str, Any]] = {}
    for row in existing:
        if isinstance(row, dict) and isinstance(row.get("round"), int):
            merged[row["round"]] = row
    for row in updates:
        merged[row["round"]] = row
    return [merged[key] for key in sorted(merged)]


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")

    season = int(args.season)
    calendar = load_calendar(Path(args.calendar_cache_dir), season)
    if not calendar:
        LOGGER.warning("No cached calendar available for season %s; skipping tyre collection.", season)
        return 0

    output_path = Path(args.output_dir) / f"season_{season}.json"
    existing = load_json(output_path) if output_path.exists() else []
    if not isinstance(existing, list):
        existing = []

    urls = load_sources(Path(args.source_config), season)
    if not urls:
        LOGGER.info("No tyre source URLs configured for season %s.", season)
        write_json(output_path, existing)
        return 0

    collected: list[dict[str, Any]] = []
    for url in urls:
        try:
            raw_html = fetch_html(url, timeout=args.timeout)
        except Exception as exc:
            LOGGER.warning("Skipping tyre source %s: %s", url, exc)
            continue
        title = extract_title(raw_html, url)
        text = html_to_text(raw_html)
        for event in calendar:
            aliases = event_aliases(event)
            compounds = extract_compounds_for_event(text, aliases)
            if not compounds:
                continue
            round_number = int(event.get("round") or 0)
            if round_number <= 0:
                continue
            collected.append(
                {
                    "round": round_number,
                    "event_name": str(event.get("event_name") or ""),
                    "compounds": compounds,
                    "source_url": url,
                    "source_title": title,
                }
            )

    if not collected:
        LOGGER.info("No new tyre compounds detected from configured sources.")
        if output_path.exists():
            return 0
        write_json(output_path, [])
        return 0

    merged = merge_entries(existing, collected)
    write_json(output_path, merged)
    LOGGER.info("Wrote tyre compounds: %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
