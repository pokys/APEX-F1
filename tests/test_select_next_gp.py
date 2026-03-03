from __future__ import annotations

from datetime import date, datetime

from pipeline.select_next_gp import apply_track_profile, next_event_from_calendar, utc_iso_timestamp


def test_next_event_from_calendar_selects_first_future_event() -> None:
    calendar = [
        {"event_date": "2026-03-01", "event_name": "Old GP"},
        {"event_date": "2026-03-08", "event_name": "Australian Grand Prix"},
        {"event_date": "2026-03-22", "event_name": "Chinese Grand Prix"},
    ]
    event = next_event_from_calendar(calendar, as_of=date(2026, 3, 3))
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
