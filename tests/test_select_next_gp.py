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
