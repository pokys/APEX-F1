from __future__ import annotations

import pytest

from pipeline.ingest_fastf1 import ScheduleUnavailableError, extract_lap_metrics, fetch_schedule


class FakeLaps:
    empty = False

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def iterrows(self):
        for index, row in enumerate(self.rows):
            yield index, row


class FakeSession:
    def __init__(self, rows: list[dict]) -> None:
        self.laps = FakeLaps(rows)


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
    failures = {"fastf1": 2}  # fail twice, succeed on attempt 3

    def fetcher(season: int, backend: str) -> str:
        attempts.append(backend)
        if failures.get(backend, 0) > 0:
            failures[backend] -= 1
            raise RuntimeError("transient")
        return f"ok-{backend}"

    result = fetch_schedule(2026, fetcher=fetcher, retries=3, sleep_fn=lambda _: None)
    assert result == "ok-fastf1"
    assert attempts == ["fastf1", "fastf1", "fastf1"]


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


def test_extract_lap_metrics_filters_unclean_laps_and_computes_gaps() -> None:
    session = FakeSession(
        [
            {"Driver": "RUS", "Team": "Mercedes", "LapTime": "0 days 00:01:20.000000", "IsAccurate": True, "Deleted": False, "PitInTime": None, "PitOutTime": None, "TrackStatus": "1"},
            {"Driver": "RUS", "Team": "Mercedes", "LapTime": "0 days 00:01:21.000000", "IsAccurate": True, "Deleted": False, "PitInTime": None, "PitOutTime": None, "TrackStatus": "1"},
            {"Driver": "ANT", "Team": "Mercedes", "LapTime": "0 days 00:01:20.500000", "IsAccurate": True, "Deleted": False, "PitInTime": None, "PitOutTime": None, "TrackStatus": "1"},
            {"Driver": "ANT", "Team": "Mercedes", "LapTime": "0 days 00:01:30.000000", "IsAccurate": True, "Deleted": False, "PitInTime": "pit", "PitOutTime": None, "TrackStatus": "1"},
        ]
    )

    metrics = extract_lap_metrics(session)
    by_driver = {row["abbreviation"]: row for row in metrics}

    assert by_driver["RUS"]["median_clean_lap_time"] == 80.5
    assert by_driver["ANT"]["median_clean_lap_time"] == 80.5
    assert by_driver["RUS"]["pace_gap_to_best_seconds"] == 0.0
    assert by_driver["ANT"]["pace_gap_to_best_seconds"] == 0.0
    assert by_driver["ANT"]["clean_lap_count"] == 1


from datetime import date

import pipeline.ingest_fastf1 as ingest_module
from pipeline.ingest_fastf1 import classify_by_best_lap, load_session, reusable_session


class FakeFrame:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.empty = not rows

    def iterrows(self):
        for index, row in enumerate(self.rows):
            yield index, row


class FakeLoadableSession:
    def __init__(self, results: list[dict], laps: list[dict]) -> None:
        self.results = FakeFrame(results)
        self._laps_rows = laps
        self.laps = FakeFrame([])
        self.name = "Practice 1"
        self.date = None
        self.load_kwargs: dict = {}

    def load(self, **kwargs) -> None:
        self.load_kwargs = kwargs
        if kwargs.get("laps"):
            self.laps = FakeFrame(self._laps_rows)


def _entry(abbr: str, team: str) -> dict:
    return {"Abbreviation": abbr, "TeamName": team, "Position": None, "DriverNumber": abbr}


def _lap(abbr: str, team: str, seconds: float, deleted: bool = False) -> dict:
    return {
        "Driver": abbr,
        "Team": team,
        "LapTime": seconds,
        "Deleted": deleted,
        "IsAccurate": True,
        "PitInTime": None,
        "PitOutTime": None,
        "TrackStatus": "1",
    }


def test_load_session_classifies_practice_from_laps(monkeypatch) -> None:
    session = FakeLoadableSession(
        results=[_entry("RUS", "Mercedes"), _entry("VER", "Red Bull"), _entry("ALB", "Williams")],
        laps=[
            _lap("RUS", "Mercedes", 81.0),
            _lap("VER", "Red Bull", 80.5),
            _lap("VER", "Red Bull", 79.0, deleted=True),
        ],
    )

    class FakeFastF1:
        @staticmethod
        def get_session(season, round_number, code):
            return session

    monkeypatch.setattr(ingest_module, "fastf1", FakeFastF1)
    payload = load_session(2026, 1, "FP1", cutoff=date(2026, 12, 31))

    assert session.load_kwargs["laps"] is True
    assert payload is not None
    by_driver = {row["abbreviation"]: row for row in payload["results"]}
    assert by_driver["VER"]["position"] == 1
    assert by_driver["VER"]["best_lap_seconds"] == 80.5
    assert by_driver["RUS"]["position"] == 2
    assert by_driver["ALB"]["position"] is None
    assert payload["lap_metrics"]


def test_load_session_skips_entry_list_without_classification(monkeypatch) -> None:
    session = FakeLoadableSession(results=[_entry("RUS", "Mercedes")], laps=[])

    class FakeFastF1:
        @staticmethod
        def get_session(season, round_number, code):
            return session

    monkeypatch.setattr(ingest_module, "fastf1", FakeFastF1)
    assert load_session(2026, 1, "SQ", cutoff=date(2026, 12, 31)) is None
    assert session.load_kwargs["laps"] is True
    assert session.load_kwargs["messages"] is True


def test_classify_by_best_lap_keeps_official_positions() -> None:
    records = [
        {"abbreviation": "RUS", "position": 2},
        {"abbreviation": "VER", "position": 1},
    ]
    out = classify_by_best_lap(records, {"RUS": 80.0, "VER": 81.0})
    assert {row["abbreviation"]: row["position"] for row in out} == {"RUS": 2, "VER": 1}
    assert {row["abbreviation"]: row["best_lap_seconds"] for row in out} == {"RUS": 80.0, "VER": 81.0}


def test_reusable_session_requires_final_classified_data() -> None:
    classified = {"results": [{"abbreviation": "RUS", "position": 1}], "lap_metrics": [{"abbreviation": "RUS"}]}
    entry_list = {"results": [{"abbreviation": "RUS", "position": None}]}
    cutoff = date(2026, 10, 7)

    assert reusable_session(classified, "R", date(2026, 9, 1), cutoff, include_lap_metrics=False)
    assert reusable_session(classified, "FP1", date(2026, 9, 1), cutoff, include_lap_metrics=False)
    # Practice stored without positions (old snapshots) must be fetched again.
    assert not reusable_session(entry_list, "FP1", date(2026, 9, 1), cutoff, include_lap_metrics=False)
    # Recent events can still change (stewards), so they are re-fetched.
    assert not reusable_session(classified, "R", date(2026, 10, 5), cutoff, include_lap_metrics=False)
    # Lap-data sessions without stored lap metrics are re-fetched.
    assert not reusable_session({"results": classified["results"]}, "SQ", date(2026, 9, 1), cutoff, include_lap_metrics=False)


def test_load_session_survives_missing_timing_data(monkeypatch) -> None:
    # Regression: FastF1 raises DataNotLoadedError on .laps when the live
    # timing API has no data; that must not abort the whole ingest.
    class NoTimingSession(FakeLoadableSession):
        @property
        def laps(self):
            raise RuntimeError("The data you are trying to access has not been loaded yet.")

        @laps.setter
        def laps(self, value):
            pass

    session = NoTimingSession(results=[_entry("RUS", "Mercedes")], laps=[])

    class FakeFastF1:
        @staticmethod
        def get_session(season, round_number, code):
            return session

    monkeypatch.setattr(ingest_module, "fastf1", FakeFastF1)
    assert load_session(2026, 1, "FP1", cutoff=date(2026, 12, 31)) is None
