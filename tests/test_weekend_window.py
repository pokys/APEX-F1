from __future__ import annotations

from datetime import datetime, timezone

from pipeline.weekend_window import in_weekend_window

SCHEDULE = {"FP1": "2026-10-09T08:30:00+00:00", "SQ": "2026-10-09T12:30:00+00:00", "R": "2026-10-11T12:00:00+00:00"}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_window_spans_the_race_weekend() -> None:
    assert not in_weekend_window(SCHEDULE, at("2026-10-08T08:00:00"))
    assert in_weekend_window(SCHEDULE, at("2026-10-08T09:00:00"))  # a day before FP1
    assert in_weekend_window(SCHEDULE, at("2026-10-10T03:00:00"))
    assert in_weekend_window(SCHEDULE, at("2026-10-11T17:30:00"))  # race result gets scored
    assert not in_weekend_window(SCHEDULE, at("2026-10-11T18:30:00"))
    assert not in_weekend_window({}, at("2026-10-10T03:00:00"))
    assert not in_weekend_window(None, at("2026-10-10T03:00:00"))
