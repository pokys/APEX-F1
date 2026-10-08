from __future__ import annotations

from datetime import date, datetime

from pathlib import Path

from pipeline.select_next_gp import (
    apply_track_profile,
    extract_sessions_schedule,
    load_cached_calendar,
    next_event_from_calendar,
    normalize_event_format,
    session_code_from_name,
    utc_iso_timestamp,
)


def test_next_event_from_calendar_selects_first_future_event(tmp_path: Path) -> None:
    calendar = [
        {"event_date": "2026-03-01", "event_name": "Old GP"},
        {"event_date": "2026-03-08", "event_name": "Australian Grand Prix"},
        {"event_date": "2026-03-22", "event_name": "Chinese Grand Prix"},
    ]
    event = next_event_from_calendar(calendar, as_of=date(2026, 3, 3), raw_dir=tmp_path)
    assert event is not None
    assert event["event_name"] == "Australian Grand Prix"


def test_apply_track_profile_applies_event_profile() -> None:
    config = {
        "overtaking_difficulty": 0.5,
        "safety_car_probability": 0.2,
        "track": {"qualifying_noise": 2.6, "race_noise": 3.8, "tyre_degradation_factor": 0.5},
    }
    event = {"event_name": "Australian Grand Prix", "country": "Australia"}
    profiles = {
        "by_event_name": {
            "australian grand prix": {
                "overtaking_difficulty": 0.62,
                "safety_car_probability": 0.38,
                "track": {"qualifying_noise": 2.4},
            }
        },
        "by_country": {},
    }

    key = apply_track_profile(config, event, profiles)
    assert key == "event:australian grand prix"
    assert config["overtaking_difficulty"] == 0.62
    assert config["safety_car_probability"] == 0.38
    assert config["track"]["qualifying_noise"] == 2.4


def test_utc_iso_timestamp_keeps_seconds_and_z_suffix() -> None:
    text = utc_iso_timestamp(datetime.fromisoformat("2026-03-03T22:31:45+00:00"))
    assert text == "2026-03-03T22:31:45Z"


def test_load_cached_calendar_and_pick_china(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cal"
    cache_dir.mkdir()
    (cache_dir / "season_2026.json").write_text(
        '[{"season":2026,"round":1,"event_name":"Australian Grand Prix","event_date":"2026-03-08","event_format":"conventional"},'
        '{"season":2026,"round":2,"event_name":"Chinese Grand Prix","event_date":"2026-03-15","event_format":"sprint"}]',
        encoding="utf-8",
    )
    calendar = load_cached_calendar(cache_dir, 2026)
    event = next_event_from_calendar(calendar, as_of=date(2026, 3, 10), raw_dir=tmp_path)
    assert event is not None
    assert event["event_name"] == "Chinese Grand Prix"
    assert event["event_format"] == "sprint"


def test_normalize_event_format_collapses_fastf1_string_variants() -> None:
    # FastF1 3.8 emits "sprint_qualifying"; older versions emit "sprint".
    # We collapse both to "sprint" so downstream stays stable.
    assert normalize_event_format("sprint_qualifying") == "sprint"
    assert normalize_event_format("sprint") == "sprint"
    assert normalize_event_format("Sprint Shootout") == "sprint"
    assert normalize_event_format("conventional") == "conventional"
    assert normalize_event_format("") == ""
    assert normalize_event_format(None) == ""


def test_session_code_from_name_maps_fastf1_display_names() -> None:
    assert session_code_from_name("Practice 1") == "FP1"
    assert session_code_from_name("Practice 3") == "FP3"
    assert session_code_from_name("Sprint Qualifying") == "SQ"
    assert session_code_from_name("Sprint Shootout") == "SQ"
    assert session_code_from_name("Sprint") == "S"
    assert session_code_from_name("Qualifying") == "Q"
    assert session_code_from_name("Race") == "R"
    assert session_code_from_name("") is None
    assert session_code_from_name(None) is None
    assert session_code_from_name("Unknown") is None


def test_extract_sessions_schedule_pulls_named_sessions_from_row() -> None:
    # FastF1 schedule rows expose Session1..Session5 (display names) plus
    # Session1DateUtc..Session5DateUtc (UTC timestamps).
    row = {
        "Session1": "Practice 1",
        "Session1DateUtc": "2026-05-01T20:30:00+00:00",
        "Session2": "Sprint Qualifying",
        "Session2DateUtc": "2026-05-02T00:30:00+00:00",
        "Session3": "Sprint",
        "Session3DateUtc": "2026-05-02T20:00:00+00:00",
        "Session4": "Qualifying",
        "Session4DateUtc": "2026-05-03T00:00:00+00:00",
        "Session5": "Race",
        "Session5DateUtc": "2026-05-03T19:30:00+00:00",
    }
    schedule = extract_sessions_schedule(row)
    assert sorted(schedule.keys()) == ["FP1", "Q", "R", "S", "SQ"]
    assert schedule["SQ"] == "2026-05-02T00:30:00+00:00"


def test_extract_sessions_schedule_falls_back_to_local_date_when_utc_missing() -> None:
    row = {
        "Session1": "Practice 1",
        "Session1Date": "2026-05-01T20:30:00+00:00",
    }
    schedule = extract_sessions_schedule(row)
    assert schedule == {"FP1": "2026-05-01T20:30:00+00:00"}


def test_get_available_sessions_ignores_entry_lists(tmp_path) -> None:
    import json

    from pipeline.select_next_gp import get_available_sessions

    snapshot = {
        "events": [
            {
                "event_name": "Singapore Grand Prix",
                "sessions": [
                    {"session_code": "FP1", "results": [{"abbreviation": "RUS", "position": None}]},
                    {"session_code": "SQ", "results": [{"abbreviation": "RUS", "position": 1}]},
                ],
            }
        ]
    }
    (tmp_path / "season_2026.json").write_text(json.dumps(snapshot), encoding="utf-8")
    assert get_available_sessions(tmp_path, 2026, "Singapore Grand Prix") == ["SQ"]


def test_live_calendar_prefers_fastf1_backend_with_exact_times(monkeypatch) -> None:
    import pipeline.select_next_gp as module

    calls: list[str] = []

    class FakeSchedule:
        def __init__(self, rows):
            self.rows = rows

        def sort_values(self, **kwargs):
            return self

        def iterrows(self):
            for idx, row in enumerate(self.rows):
                yield idx, row

    class FakeFastF1:
        @staticmethod
        def get_event_schedule(season, include_testing=False, backend=None):
            calls.append(backend)
            return FakeSchedule(
                [
                    {
                        "EventDate": "2026-10-11",
                        "RoundNumber": 17,
                        "EventName": "Singapore Grand Prix",
                        "EventFormat": "sprint_qualifying",
                        "Country": "Singapore",
                        "Session1": "Practice 1",
                        "Session1DateUtc": "2026-10-09 08:30:00",
                        "Session2": "Sprint Qualifying",
                        "Session2DateUtc": "2026-10-09 12:30:00",
                    }
                ]
            )

    monkeypatch.setattr(module, "fastf1", FakeFastF1)
    calendar = module.fetch_live_calendar(2026)
    assert calls == ["fastf1"]
    assert calendar[0]["sessions_schedule"]["SQ"].startswith("2026-10-09T12:30:00")


def test_merge_calendars_keeps_exact_times_over_date_only_values() -> None:
    from pipeline.select_next_gp import merge_calendars

    old = [{"round": 17, "event_name": "Singapore Grand Prix", "sessions_schedule": {"SQ": "2026-10-09T12:30:00+00:00", "Q": "2026-10-10T13:00:00+00:00"}}]
    new = [{"round": 17, "event_name": "Singapore Grand Prix", "sessions_schedule": {"SQ": "2026-10-09T00:00:00+00:00", "Q": "2026-10-11T00:00:00+00:00"}}]
    merged = merge_calendars(new, old)[0]["sessions_schedule"]
    assert merged["SQ"] == "2026-10-09T12:30:00+00:00"
    # A moved session (different day) is taken from the fresh schedule.
    assert merged["Q"] == "2026-10-11T00:00:00+00:00"


def test_track_params_reset_between_events_and_country_alias() -> None:
    from pipeline.select_next_gp import DEFAULT_TRACK_PARAMS, apply_track_profile, reset_track_params

    profiles = {
        "by_event_name": {},
        "by_country": {
            "singapore": {"safety_car_probability": 0.52, "overtaking_difficulty": 0.79, "track": {"race_noise": 3.5}},
            "united kingdom": {"safety_car_probability": 0.3, "track": {"race_noise": 3.2}},
        },
    }
    config: dict = {}
    reset_track_params(config)
    assert apply_track_profile(config, {"event_name": "Singapore Grand Prix", "country": "Singapore"}, profiles) == "country:singapore"
    config["track_profile"] = "country:singapore"

    # Next GP without any profile must not inherit Singapore's parameters.
    reset_track_params(config)
    assert apply_track_profile(config, {"event_name": "Mystery Grand Prix", "country": "Atlantis"}, profiles) is None
    assert config["safety_car_probability"] == DEFAULT_TRACK_PARAMS["safety_car_probability"]
    assert config["overtaking_difficulty"] == DEFAULT_TRACK_PARAMS["overtaking_difficulty"]
    assert "track_profile" not in config

    # FastF1 reports "UK"; the profile key is the full name.
    reset_track_params(config)
    assert apply_track_profile(config, {"event_name": "British Grand Prix", "country": "UK"}, profiles) == "country:united kingdom"
    assert config["track"]["race_noise"] == 3.2


def test_every_2026_calendar_event_has_a_track_profile() -> None:
    import json
    from pathlib import Path

    from pipeline.select_next_gp import apply_track_profile, load_track_profiles, reset_track_params

    root = Path(__file__).resolve().parents[1]
    profiles = load_track_profiles(root / "config" / "track_profiles.json")
    calendar = json.loads((root / "data" / "raw" / "calendars" / "season_2026.json").read_text(encoding="utf-8"))
    missing = []
    for event in calendar:
        config: dict = {}
        reset_track_params(config)
        if apply_track_profile(config, event, profiles) is None:
            missing.append(event["event_name"])
    assert missing == []
