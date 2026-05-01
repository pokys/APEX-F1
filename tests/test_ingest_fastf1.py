from __future__ import annotations

import pytest

from pipeline.ingest_fastf1 import ScheduleUnavailableError, fetch_schedule


def test_fetch_schedule_returns_first_success() -> None:
    calls: list[tuple[int, str]] = []

    def fetcher(season: int, backend: str) -> str:
        calls.append((season, backend))
        return f"schedule-{season}-{backend}"

    result = fetch_schedule(2026, fetcher=fetcher, backends=("f1timing", "ergast"), retries=3, sleep_fn=lambda _: None)
    assert result == "schedule-2026-f1timing"
    assert calls == [(2026, "f1timing")]


def test_fetch_schedule_falls_back_to_next_backend() -> None:
    seen: list[str] = []

    def fetcher(season: int, backend: str) -> str:
        seen.append(backend)
        if backend == "f1timing":
            raise RuntimeError("f1timing down")
        return "ergast-ok"

    result = fetch_schedule(2026, fetcher=fetcher, backends=("f1timing", "ergast"), retries=2, sleep_fn=lambda _: None)
    assert result == "ergast-ok"
    # f1timing tried twice (retries=2), then ergast picked up on first try.
    assert seen == ["f1timing", "f1timing", "ergast"]


def test_fetch_schedule_retries_within_backend_before_falling_back() -> None:
    attempts: list[str] = []
    failures = {"f1timing": 2}  # fail twice, succeed on attempt 3

    def fetcher(season: int, backend: str) -> str:
        attempts.append(backend)
        if failures.get(backend, 0) > 0:
            failures[backend] -= 1
            raise RuntimeError("transient")
        return f"ok-{backend}"

    result = fetch_schedule(2026, fetcher=fetcher, retries=3, sleep_fn=lambda _: None)
    assert result == "ok-f1timing"
    assert attempts == ["f1timing", "f1timing", "f1timing"]


def test_fetch_schedule_raises_after_all_backends_exhausted() -> None:
    sleeps: list[float] = []

    def fetcher(season: int, backend: str) -> str:
        raise RuntimeError("everything is broken")

    with pytest.raises(ScheduleUnavailableError) as exc_info:
        fetch_schedule(
            2026,
            fetcher=fetcher,
            backends=("f1timing", "ergast"),
            retries=3,
            sleep_fn=sleeps.append,
            base_delay_seconds=1.0,
        )

    assert "season 2026" in str(exc_info.value)
    # 2 backends * (retries-1 sleeps each) = 4 sleeps. Backoff schedule: 1, 2.
    assert sleeps == [1.0, 2.0, 1.0, 2.0]
