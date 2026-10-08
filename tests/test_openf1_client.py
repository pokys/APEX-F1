from __future__ import annotations

from pipeline.ingest_fastf1 import load_session_openf1
from pipeline.openf1_client import OpenF1Client

SESSIONS = [
    {"session_key": 9001, "session_name": "Practice 1", "date_start": "2026-10-09T08:30:00+00:00", "year": 2026},
    {"session_key": 9002, "session_name": "Sprint Qualifying", "date_start": "2026-10-09T12:30:00+00:00", "year": 2026},
    {"session_key": 8002, "session_name": "Sprint Qualifying", "date_start": "2026-07-03T15:30:00+00:00", "year": 2026},
]
DRIVERS = [
    {"driver_number": 63, "name_acronym": "RUS", "team_name": "Mercedes", "full_name": "George RUSSELL"},
    {"driver_number": 1, "name_acronym": "VER", "team_name": "Red Bull Racing", "full_name": "Max VERSTAPPEN"},
]


def make_fetcher(results_by_key: dict, laps_by_key: dict | None = None):
    calls: list[tuple[str, dict]] = []

    def fetch(endpoint: str, params: dict):
        calls.append((endpoint, params))
        if endpoint == "sessions":
            return SESSIONS
        if endpoint == "drivers":
            return DRIVERS
        if endpoint == "session_result":
            return results_by_key.get(params["session_key"], [])
        if endpoint == "laps":
            return (laps_by_key or {}).get(params["session_key"], [])
        return []

    return fetch, calls


def test_find_session_key_matches_name_and_start_time() -> None:
    fetch, _ = make_fetcher({})
    client = OpenF1Client(fetcher=fetch)
    assert client.find_session_key(2026, "SQ", "2026-10-09T12:30:00+00:00") == 9002
    assert client.find_session_key(2026, "SQ", "2026-07-03T15:30:00+00:00") == 8002
    assert client.find_session_key(2026, "FP2", "2026-10-09T12:30:00+00:00") is None


def test_sprint_qualifying_results_keep_segment_times() -> None:
    fetch, _ = make_fetcher(
        {
            9002: [
                {"position": 1, "driver_number": 63, "duration": [91.2, 90.8, 90.1]},
                {"position": 2, "driver_number": 1, "duration": [91.0, 90.9, 90.3]},
            ]
        }
    )
    payload = load_session_openf1(OpenF1Client(fetcher=fetch), 2026, "SQ", "2026-10-09T12:30:00+00:00")
    assert payload is not None and payload["source"] == "openf1"
    rows = payload["results"]
    assert [row["abbreviation"] for row in rows] == ["RUS", "VER"]
    assert rows[0]["q3"] == "0 days 00:01:30.100000"
    assert rows[0]["team_name"] == "Mercedes"


def test_practice_without_results_is_ranked_by_fastest_lap() -> None:
    fetch, _ = make_fetcher(
        {},
        {9001: [
            {"driver_number": 63, "lap_duration": 92.5},
            {"driver_number": 1, "lap_duration": 92.1},
            {"driver_number": 1, "lap_duration": None},
        ]},
    )
    payload = load_session_openf1(OpenF1Client(fetcher=fetch), 2026, "FP1", "2026-10-09T08:30:00+00:00")
    assert payload is not None
    positions = {row["abbreviation"]: row["position"] for row in payload["results"]}
    assert positions == {"VER": 1, "RUS": 2}


def test_openf1_failure_returns_none() -> None:
    def fetch(endpoint, params):
        raise RuntimeError("network down")

    assert load_session_openf1(OpenF1Client(fetcher=fetch), 2026, "SQ", "2026-10-09T12:30:00+00:00") is None


def test_align_team_names_uses_fastf1_roster() -> None:
    from pipeline.ingest_fastf1 import align_team_names, update_roster

    roster: dict[str, str] = {}
    update_roster(roster, {"results": [{"abbreviation": "VER", "team_name": "Red Bull"}]})
    update_roster(roster, {"source": "openf1", "results": [{"abbreviation": "VER", "team_name": "Red Bull Racing"}]})
    rows = align_team_names([{"abbreviation": "VER", "team_name": "Red Bull Racing"}], roster)
    assert rows[0]["team_name"] == "Red Bull"
