#!/usr/bin/env python3
"""
Collect Formula 1 news articles from RSS/Atom feeds into a markdown inbox.

Deterministic/idempotent behavior:
- Feed URLs are processed in stable order from feeds.yaml.
- Article entries are deduplicated by sha256(title + url).
- Existing inbox entries are preserved, including human checkbox state.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


LOGGER = logging.getLogger("collect_articles")

INBOX_HEADER = "# F1 Article Inbox"
ARTICLE_LINE_RE = re.compile(r"^- \[[ xX]\] (?P<title>.+) \((?P<url>https?://[^)]+)\)\s*$")


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    published_date: date
    article_hash: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect F1 RSS articles into markdown inbox.")
    parser.add_argument(
        "--feeds",
        default="knowledge/feeds.yaml",
        help="Path to feeds YAML file (default: knowledge/feeds.yaml).",
    )
    parser.add_argument(
        "--inbox",
        default="knowledge/inbox/articles.md",
        help="Path to inbox markdown file (default: knowledge/inbox/articles.md).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="HTTP timeout in seconds per feed (default: 20).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args()


def normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


def make_article_hash(title: str, url: str) -> str:
    raw = f"{normalize_text(title)}{normalize_text(url)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_feed_urls(feeds_path: Path) -> list[str]:
    if not feeds_path.exists():
        raise FileNotFoundError(f"Feeds file not found: {feeds_path}")

    raw_text = feeds_path.read_text(encoding="utf-8")

    data: Any | None = None
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw_text)
    except ModuleNotFoundError:
        LOGGER.warning("PyYAML not installed; using minimal YAML parser fallback.")
    except Exception as exc:
        raise ValueError(f"Could not parse YAML from {feeds_path}: {exc}") from exc

    urls = extract_feed_urls(data) if data is not None else extract_feed_urls_fallback(raw_text)
    if not urls:
        raise ValueError(f"No RSS feed URLs found in {feeds_path}")

    # Stable dedupe while preserving configured order.
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def extract_feed_urls(data: Any) -> list[str]:
    if isinstance(data, dict):
        feeds_data = data.get("feeds", data)
        if isinstance(feeds_data, dict):
            feeds_data = list(feeds_data.values())
    else:
        feeds_data = data

    if not isinstance(feeds_data, list):
        raise ValueError("feeds.yaml must contain a list of feed entries or a 'feeds' list.")

    urls: list[str] = []
    for entry in feeds_data:
        if isinstance(entry, str):
            value = entry.strip()
            if value:
                urls.append(value)
            continue
        if isinstance(entry, dict):
            url = entry.get("url") or entry.get("rss")
            if isinstance(url, str) and url.strip():
                urls.append(url.strip())
                continue
        raise ValueError(f"Unsupported feed entry format: {entry!r}")
    return urls


def extract_feed_urls_fallback(raw_text: str) -> list[str]:
    urls: list[str] = []
    in_list_item = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line == "feeds:":
            continue
        if line.startswith("-"):
            in_list_item = True
            value = line[1:].strip()
            if value.startswith("url:"):
                value = value.split(":", 1)[1].strip().strip("\"'")
                if value:
                    urls.append(value)
                continue
            if value.startswith(("http://", "https://")):
                urls.append(value.strip("\"'"))
                continue
            continue

        if not in_list_item:
            continue
        if line.startswith("url:"):
            value = line.split(":", 1)[1].strip().strip("\"'")
            if value:
                urls.append(value)
    return urls


def fetch_feed_xml(feed_url: str, timeout_seconds: int) -> bytes:
    request = Request(
        feed_url,
        headers={"User-Agent": "APEX-F1/1.0 (+https://github.com/)"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return response.read()


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def child_text(node: ET.Element, names: set[str]) -> str | None:
    for child in node:
        if local_name(child.tag) in names:
            if child.text and child.text.strip():
                return child.text.strip()
    return None


def atom_link(entry: ET.Element) -> str | None:
    fallback: str | None = None
    for child in entry:
        if local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if not href:
            continue
        rel = (child.attrib.get("rel") or "alternate").strip()
        if rel == "alternate":
            return href
        if fallback is None:
            fallback = href
    return fallback


def parse_datetime_value(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None

    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    candidates = [value]
    if value.endswith("Z"):
        candidates.append(f"{value[:-1]}+00:00")
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def parse_feed_entries(feed_xml: bytes, fallback_date: date) -> list[tuple[str, str, date]]:
    root = ET.fromstring(feed_xml)
    root_name = local_name(root.tag).lower()

    parsed: list[tuple[str, str, date]] = []

    if root_name == "feed":
        entries = [node for node in root if local_name(node.tag) == "entry"]
        for entry in entries:
            title = child_text(entry, {"title"})
            link = atom_link(entry)
            published_raw = child_text(entry, {"published", "updated", "date"})
            if not title or not link:
                continue
            dt = parse_datetime_value(published_raw)
            parsed.append((normalize_text(title), link.strip(), (dt.date() if dt else fallback_date)))
        return parsed

    items: list[ET.Element] = []
    for node in root.iter():
        if local_name(node.tag) == "item":
            items.append(node)

    for item in items:
        title = child_text(item, {"title"})
        link = child_text(item, {"link"})
        published_raw = child_text(item, {"pubDate", "published", "updated", "date"})
        if not title or not link:
            continue
        dt = parse_datetime_value(published_raw)
        parsed.append((normalize_text(title), link.strip(), (dt.date() if dt else fallback_date)))

    return parsed


def load_existing_hashes(inbox_path: Path) -> tuple[list[str], set[str]]:
    if not inbox_path.exists():
        return [INBOX_HEADER, ""], set()

    lines = inbox_path.read_text(encoding="utf-8").splitlines()
    hashes: set[str] = set()
    for line in lines:
        match = ARTICLE_LINE_RE.match(line.strip())
        if not match:
            continue
        hashes.add(make_article_hash(match.group("title"), match.group("url")))
    if not lines:
        lines = [INBOX_HEADER, ""]
    return lines, hashes


def ensure_inbox_header(lines: list[str]) -> list[str]:
    if lines and lines[0].strip() == INBOX_HEADER:
        return lines
    if not lines:
        return [INBOX_HEADER, ""]
    return [INBOX_HEADER, ""] + lines


def find_section_bounds(lines: list[str], section_date: str) -> tuple[int, int] | None:
    section_header = f"## {section_date}"
    start_index: int | None = None
    for idx, line in enumerate(lines):
        if line.strip() == section_header:
            start_index = idx
            break
    if start_index is None:
        return None

    end_index = len(lines)
    for idx in range(start_index + 1, len(lines)):
        if lines[idx].startswith("## "):
            end_index = idx
            break
    return start_index, end_index


def insert_articles(lines: list[str], grouped_articles: dict[str, list[Article]]) -> list[str]:
    updated = ensure_inbox_header(list(lines))

    for section_date in sorted(grouped_articles.keys()):
        new_lines = [f"- [ ] {article.title} ({article.url})" for article in grouped_articles[section_date]]
        bounds = find_section_bounds(updated, section_date)
        if bounds is None:
            if updated and updated[-1] != "":
                updated.append("")
            updated.append(f"## {section_date}")
            updated.extend(new_lines)
            continue

        start, end = bounds
        insert_at = end
        # Keep section trailing blank lines intact, append entries before them.
        while insert_at > start + 1 and updated[insert_at - 1].strip() == "":
            insert_at -= 1
        updated[insert_at:insert_at] = new_lines

    if updated and updated[-1] != "":
        updated.append("")
    return updated


def collect_articles(feed_urls: list[str], timeout_seconds: int, existing_hashes: set[str]) -> list[Article]:
    fallback_date = datetime.now(timezone.utc).date()
    seen_hashes = set(existing_hashes)
    new_articles: list[Article] = []

    for feed_url in feed_urls:
        try:
            feed_xml = fetch_feed_xml(feed_url, timeout_seconds=timeout_seconds)
            entries = parse_feed_entries(feed_xml, fallback_date=fallback_date)
        except (HTTPError, URLError, TimeoutError, ET.ParseError, ValueError) as exc:
            LOGGER.warning("Skipping feed %s due to fetch/parse error: %s", feed_url, exc)
            continue
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.warning("Skipping feed %s due to unexpected error: %s", feed_url, exc)
            continue

        for title, url, published_date in entries:
            article_hash = make_article_hash(title, url)
            if article_hash in seen_hashes:
                continue
            seen_hashes.add(article_hash)
            new_articles.append(
                Article(
                    title=title,
                    url=url,
                    published_date=published_date,
                    article_hash=article_hash,
                )
            )

    new_articles.sort(key=lambda x: (x.published_date.isoformat(), x.title.lower(), x.url))
    return new_articles


def group_articles_by_date(articles: list[Article]) -> dict[str, list[Article]]:
    grouped: dict[str, list[Article]] = {}
    for article in articles:
        date_key = article.published_date.isoformat()
        grouped.setdefault(date_key, []).append(article)
    return grouped


def run(feeds_path: Path, inbox_path: Path, timeout_seconds: int) -> int:
    feed_urls = load_feed_urls(feeds_path)
    inbox_lines, existing_hashes = load_existing_hashes(inbox_path)

    new_articles = collect_articles(feed_urls, timeout_seconds=timeout_seconds, existing_hashes=existing_hashes)
    if not new_articles:
        LOGGER.info("No new articles found.")
        return 0

    grouped = group_articles_by_date(new_articles)
    updated_lines = insert_articles(inbox_lines, grouped)

    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text("\n".join(updated_lines), encoding="utf-8")

    LOGGER.info("Added %d new article(s) from %d feed(s).", len(new_articles), len(feed_urls))
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        return run(
            feeds_path=Path(args.feeds),
            inbox_path=Path(args.inbox),
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        LOGGER.error("collect_articles failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
