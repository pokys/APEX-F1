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


def test_ingest_workflows_restore_fastf1_cache_before_ingest() -> None:
    from pathlib import Path

    for name in ("ingest-fastf1.yml", "full-pipeline.yml"):
        text = Path(".github/workflows", name).read_text(encoding="utf-8")
        assert "actions/cache@" in text and "path: data/raw/fastf1_cache" in text, name
        assert text.index("actions/cache@") < text.index("pipeline/ingest_fastf1.py \"${ARGS[@]}\""), name


def test_annotate_wet_flag_stores_openf1_result_once() -> None:
    from pipeline.ingest_fastf1 import annotate_wet_flag
    from pipeline.openf1_client import OpenF1Client

    calls: list[str] = []

    def fetch(endpoint, params):
        calls.append(endpoint)
        if endpoint == "sessions":
            return [{"session_key": 7, "session_name": "Race", "date_start": "2026-07-05T14:00:00+00:00"}]
        if endpoint == "stints":
            return [{"driver_number": 1, "compound": "INTERMEDIATE"}, {"driver_number": 4, "compound": "WET"}]
        return []

    client = OpenF1Client(fetcher=fetch)
    session = {"session_code": "R", "results": []}
    annotate_wet_flag(session, client, 2026, "R", "2026-07-05T14:00:00+00:00")
    assert session["wet"] is True
    calls.clear()
    annotate_wet_flag(session, client, 2026, "R", "2026-07-05T14:00:00+00:00")
    assert calls == []  # already known, no new request
    practice = {"session_code": "FP1"}
    annotate_wet_flag(practice, client, 2026, "FP1", "2026-07-03T11:30:00+00:00")
    assert "wet" not in practice


def test_team_names_are_canonical_across_sources() -> None:
    from pipeline.ingest_fastf1 import canonicalize_teams

    rows = canonicalize_teams([
        {"abbreviation": "VER", "team_name": "Red Bull Racing"},
        {"abbreviation": "LAW", "team_name": "Racing Bulls"},
        {"abbreviation": "GAS", "team_name": "Alpine"},
        {"abbreviation": "PER", "team_name": "Cadillac"},
        {"abbreviation": "LEC", "team_name": "Ferrari"},
        {"abbreviation": "XXX", "team_name": None},
    ])
    assert [r["team_name"] for r in rows] == ["Red Bull", "RB F1 Team", "Alpine F1 Team", "Cadillac F1 Team", "Ferrari", None]


def test_missing_team_is_filled_from_roster() -> None:
    from pipeline.ingest_fastf1 import fill_missing_teams

    rows = fill_missing_teams(
        [{"abbreviation": "RUS", "team_name": None}, {"abbreviation": "BEG", "team_name": None}, {"abbreviation": "LEC", "team_name": "Ferrari"}],
        {"RUS": "Mercedes", "LEC": "Williams"},
    )
    assert [r["team_name"] for r in rows] == ["Mercedes", None, "Ferrari"]


def test_lap_metrics_use_canonical_team_names(monkeypatch) -> None:
    session = FakeLoadableSession(
        results=[_entry("VER", "Red Bull Racing")],
        laps=[_lap("VER", "Red Bull Racing", 80.0)],
    )

    class FakeFastF1:
        @staticmethod
        def get_session(season, round_number, code):
            return session

    monkeypatch.setattr(ingest_module, "fastf1", FakeFastF1)
    payload = load_session(2026, 1, "FP1", cutoff=date(2026, 12, 31))
    assert payload["results"][0]["team_name"] == "Red Bull"
    assert payload["lap_metrics"][0]["team_name"] == "Red Bull"


def test_ingest_keeps_stored_session_when_live_sources_fail(tmp_path, monkeypatch) -> None:
    import json

    import pandas as pd

    from pipeline import ingest_fastf1 as ing

    stored = {
        "session_code": "SQ",
        "source": "fastf1",
        "results": [{"position": 1, "abbreviation": "VER", "team_name": "Red Bull"}, {"position": 2, "abbreviation": "RUS", "team_name": "Mercedes"}],
        "lap_metrics": [{"abbreviation": "VER"}],
    }
    (tmp_path / "season_2026.json").write_text(json.dumps({"events": [{"round": 17, "sessions": [stored]}]}))
    schedule = pd.DataFrame([{"RoundNumber": 17, "EventDate": pd.Timestamp("2026-10-11"), "EventName": "Singapore Grand Prix",
                              "OfficialEventName": "x", "EventFormat": "sprint_qualifying", "Country": "Singapore", "Location": "Marina Bay"}])
    monkeypatch.setattr(ing, "fetch_schedule", lambda season, fetcher: schedule)
    monkeypatch.setattr(ing, "extract_sessions_schedule", lambda row: {})
    monkeypatch.setattr(ing, "load_session", lambda *a, **k: None)  # live timing: "no data"
    monkeypatch.setattr(ing.fastf1.Cache, "enable_cache", lambda path: None)

    out = ing.ingest(2026, ["FP1", "SQ"], ing.parse_iso_date("2026-10-10"), tmp_path, tmp_path / "cache")
    events = json.loads(out.read_text())["events"]
    assert [e["round"] for e in events] == [17]
    assert [s["session_code"] for s in events[0]["sessions"]] == ["SQ"]
    assert events[0]["sessions"][0]["results"][0]["abbreviation"] == "VER"


def test_ingest_falls_back_to_openf1_for_qualifying(tmp_path, monkeypatch) -> None:
    # Regression (Singapore 2026): F1 live timing refused the GitHub runner and
    # Ergast had no results yet, so sprint and qualifying never arrived and the
    # race grid was simulated instead of taken from qualifying.
    import json

    import pandas as pd

    from pipeline import ingest_fastf1 as ing

    schedule = pd.DataFrame([{"RoundNumber": 17, "EventDate": pd.Timestamp("2026-10-11"), "EventName": "Singapore Grand Prix",
                              "OfficialEventName": "x", "EventFormat": "sprint_qualifying", "Country": "Singapore", "Location": "Marina Bay"}])
    monkeypatch.setattr(ing, "fetch_schedule", lambda season, fetcher: schedule)
    monkeypatch.setattr(ing, "extract_sessions_schedule", lambda row: {"Q": "2026-10-10T13:00:00+00:00"})
    monkeypatch.setattr(ing, "load_session", lambda *a, **k: None)
    monkeypatch.setattr(ing.fastf1.Cache, "enable_cache", lambda path: None)

    class FakeOpenF1:
        def find_session_key(self, season, code, start):
            return 42 if code == "Q" else None

        def session_results(self, key, code):
            return [{"position": 1, "abbreviation": "VER", "team_name": "Red Bull"}, {"position": 2, "abbreviation": "ANT", "team_name": "Mercedes"}]

        def best_laps(self, key):
            raise AssertionError("qualifying must not be ranked by fastest lap")

        def session_is_wet(self, key):
            return False

    out = ing.ingest(2026, ["Q"], ing.parse_iso_date("2026-10-10"), tmp_path, tmp_path / "cache", openf1=FakeOpenF1())
    sessions = json.loads(out.read_text())["events"][0]["sessions"]
    assert [(s["session_code"], s["source"]) for s in sessions] == [("Q", "openf1")]
    assert [r["abbreviation"] for r in sessions[0]["results"]] == ["VER", "ANT"]
