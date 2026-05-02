from __future__ import annotations

import json
from pathlib import Path

from pipeline.prediction_targeting import build_inputs_manifest, build_inputs_status, compute_data_freshness, compute_weekend_form, find_cached_calendar_entry, latest_completed_event, load_cached_calendar, load_session_weights, select_prediction_target, sessions_completed_by_calendar


def test_select_prediction_target_standard_weekend() -> None:
    assert select_prediction_target("standard", []) == "qualifying"
    assert select_prediction_target("standard", ["FP1", "FP2", "Q"]) == "race"


def test_select_prediction_target_sprint_weekend() -> None:
    assert select_prediction_target("sprint", []) == "sprint_qualifying"
    assert select_prediction_target("sprint", ["SQ"]) == "sprint"
    assert select_prediction_target("sprint", ["SQ", "S"]) == "qualifying"
    assert select_prediction_target("sprint", ["SQ", "S", "Q"]) == "race"


def test_build_inputs_manifest_filters_missing_sessions_and_normalizes() -> None:
    manifest = build_inputs_manifest(
        target="race",
        available_sessions=["FP3", "Q"],
        session_weights={
            "race": {
                "history_driver": 0.2,
                "history_team": 0.1,
                "qualifying": 0.4,
                "fp2": 0.1,
                "fp3": 0.1,
                "signals": 0.1,
            }
        },
        active_signal_count=0,
    )
    sources = [row["source"] for row in manifest]
    assert "qualifying" in sources
    assert "fp2" not in sources
    assert "signals" not in sources
    total = sum(float(row["weight"]) for row in manifest)
    assert round(total, 6) == 1.0


def test_cached_calendar_lookup(tmp_path: Path) -> None:
    path = tmp_path / "season_2026.json"
    path.write_text(
        json.dumps(
            [
                {"event_name": "Chinese Grand Prix", "event_format": "sprint"},
                {"event_name": "Japanese Grand Prix", "event_format": "conventional"},
            ]
        ),
        encoding="utf-8",
    )
    calendar = load_cached_calendar(path)
    chinese = find_cached_calendar_entry(calendar, "Chinese Grand Prix")
    japan = find_cached_calendar_entry(calendar, "Japanese Grand Prix")
    assert chinese is not None
    assert chinese["event_format"] == "sprint"
    assert japan is not None
    assert japan["event_format"] == "conventional"


def test_build_inputs_status_marks_used_and_missing() -> None:
    rows = build_inputs_status(
        target="sprint_qualifying",
        available_sessions=[],
        session_weights={
            "sprint_qualifying": {
                "history_driver": 0.5,
                "history_team": 0.3,
                "fp1": 0.2,
                "signals": 0.0,
            }
        },
        active_signal_count=0,
    )
    by_source = {row["source"]: row for row in rows}
    assert by_source["history_driver"]["status"] == "used"
    assert by_source["history_team"]["status"] == "used"
    assert by_source["fp1"]["status"] == "missing"
    assert by_source["signals"]["status"] == "available_zero_weight"


def test_config_session_weights_prioritize_current_inputs() -> None:
    weights = load_session_weights(Path("config/session_weights.json"))

    current_inputs = {
        "qualifying": {"fp1", "fp2", "fp3", "signals"},
        "race": {"qualifying", "fp2", "fp3", "signals"},
        "sprint_qualifying": {"fp1", "signals"},
        "sprint": {"sprint_qualifying", "fp1", "signals"},
    }

    for target, sources in current_inputs.items():
        target_weights = weights[target]
        history_weight = target_weights["history_driver"] + target_weights["history_team"]
        current_weight = sum(target_weights[source] for source in sources)
        assert current_weight > history_weight


def test_compute_weekend_form_blends_history_baseline_with_sessions() -> None:
    event = {
        "sessions": [
            {
                "session_code": "FP1",
                "results": [
                    {"position": 1, "abbreviation": "VER"},
                    {"position": 2, "abbreviation": "NOR"},
                    {"position": 3, "abbreviation": "LEC"},
                ],
            }
        ]
    }
    manifest = [
        {"source": "history_driver", "source_key": "history_driver", "weight": 0.4},
        {"source": "history_team", "source_key": "history_team", "weight": 0.2},
        {"source": "fp1", "source_key": "FP1", "weight": 0.4},
    ]

    form = compute_weekend_form("VER", event, manifest)

    assert form["sources"] == [{"session": "FP1", "weight": 0.4, "score": 1.0}]
    assert round(form["delta"], 6) == 2.0


def test_latest_completed_event_picks_most_recent_with_results() -> None:
    snapshot = {
        "events": [
            {
                "event_name": "Australian Grand Prix",
                "event_date": "2026-03-08",
                "sessions": [{"session_code": "R", "results": [{"position": 1}]}],
            },
            {
                "event_name": "Chinese Grand Prix",
                "event_date": "2026-03-15",
                "sessions": [{"session_code": "R", "results": [{"position": 1}]}],
            },
            # Future placeholder with no results - must be ignored.
            {
                "event_name": "Bahrain Grand Prix",
                "event_date": "2026-04-12",
                "sessions": [{"session_code": "R", "results": []}],
            },
        ]
    }
    name, when = latest_completed_event(snapshot)
    assert name == "Chinese Grand Prix"
    assert when.isoformat() == "2026-03-15"


def test_compute_data_freshness_flags_long_gap_as_stale() -> None:
    snapshot = {
        "events": [
            {
                "event_name": "Japanese Grand Prix",
                "event_date": "2026-03-29",
                "sessions": [{"session_code": "R", "results": [{"position": 1}]}],
            },
        ]
    }
    freshness = compute_data_freshness(
        snapshot,
        race_date="2026-05-03",
        generated_at="2026-05-01T09:00:00Z",
        stale_threshold_days=21,
    )
    assert freshness["latest_completed_event"] == "Japanese Grand Prix"
    assert freshness["latest_completed_date"] == "2026-03-29"
    assert freshness["days_since_latest_event"] == 33
    assert freshness["days_until_next_race"] == 2
    assert freshness["is_stale"] is True


def test_compute_data_freshness_recent_event_not_stale() -> None:
    snapshot = {
        "events": [
            {
                "event_name": "Miami Grand Prix",
                "event_date": "2026-05-03",
                "sessions": [{"session_code": "R", "results": [{"position": 1}]}],
            },
        ]
    }
    freshness = compute_data_freshness(
        snapshot,
        race_date="2026-05-24",
        generated_at="2026-05-08T09:00:00Z",
        stale_threshold_days=21,
    )
    assert freshness["days_since_latest_event"] == 5
    assert freshness["is_stale"] is False


def test_compute_data_freshness_no_results_is_stale() -> None:
    freshness = compute_data_freshness(
        {"events": []},
        race_date="2026-05-03",
        generated_at="2026-05-01T09:00:00Z",
        stale_threshold_days=21,
    )
    assert freshness["latest_completed_event"] is None
    assert freshness["latest_completed_date"] is None
    assert freshness["days_since_latest_event"] is None
    assert freshness["is_stale"] is True


def test_sessions_completed_by_calendar_includes_past_sessions() -> None:
    schedule = {
        "FP1": "2026-05-01T20:30:00+00:00",
        "SQ":  "2026-05-02T00:30:00+00:00",
        "S":   "2026-05-02T20:00:00+00:00",
        "Q":   "2026-05-03T00:00:00+00:00",
        "R":   "2026-05-03T19:30:00+00:00",
    }
    # Saturday afternoon UTC: FP1 + SQ are firmly done, S has just started.
    completed = sessions_completed_by_calendar(
        schedule,
        reference_time="2026-05-02T18:00:00+00:00",
        buffer_minutes=90,
    )
    assert sorted(completed) == ["FP1", "SQ"]


def test_sessions_completed_by_calendar_respects_buffer() -> None:
    # Buffer prevents the next session from being prematurely declared done
    # during the session itself.
    schedule = {"SQ": "2026-05-02T00:30:00+00:00"}
    # 60 minutes after SQ start - well within typical 60 min session window.
    not_yet = sessions_completed_by_calendar(
        schedule, reference_time="2026-05-02T01:30:00+00:00", buffer_minutes=90,
    )
    assert not_yet == []
    # 100 minutes after SQ start - SQ is reliably done.
    done = sessions_completed_by_calendar(
        schedule, reference_time="2026-05-02T02:10:00+00:00", buffer_minutes=90,
    )
    assert done == ["SQ"]


def test_sessions_completed_by_calendar_handles_empty_schedule() -> None:
    assert sessions_completed_by_calendar(None, reference_time="2026-05-02T18:00:00Z") == []
    assert sessions_completed_by_calendar({}, reference_time="2026-05-02T18:00:00Z") == []
    # bad reference_time returns []
    assert sessions_completed_by_calendar({"SQ": "2026-05-02T00:30:00+00:00"}, reference_time="not-a-date") == []


def test_sessions_completed_by_calendar_normalizes_codes() -> None:
    schedule = {"fp1": "2026-05-01T20:30:00+00:00"}
    completed = sessions_completed_by_calendar(
        schedule, reference_time="2026-05-02T18:00:00+00:00", buffer_minutes=90,
    )
    assert completed == ["FP1"]


def test_sessions_completed_by_calendar_advances_through_first_of_day() -> None:
    # FastF1's Ergast fallback collapses every same-day session to 00:00 UTC.
    # For sprint Saturday, S (1st of day) and Q (2nd of day) share the
    # midnight start. After Sprint runs (mid-Saturday UTC) but before Q,
    # the target must advance to qualifying - i.e. S "done", Q not.
    schedule = {
        "FP1": "2026-05-01T00:00:00+00:00",
        "SQ":  "2026-05-01T00:00:00+00:00",
        "S":   "2026-05-02T00:00:00+00:00",
        "Q":   "2026-05-02T00:00:00+00:00",
        "R":   "2026-05-03T20:00:00+00:00",
    }
    # Saturday 22:00 UTC: post-Sprint, pre-Quali for any timezone.
    completed = sessions_completed_by_calendar(
        schedule, reference_time="2026-05-02T22:00:00+00:00", buffer_minutes=90,
    )
    assert completed == ["FP1", "SQ", "S"]


def test_sessions_completed_by_calendar_holds_at_last_of_day_until_end_of_day() -> None:
    # The last date-only session of a day (Q on Saturday in sprint format)
    # should not be flagged done before end-of-day UTC, otherwise the
    # target races ahead to "race" before Q has actually been driven.
    schedule = {
        "FP1": "2026-05-01T00:00:00+00:00",
        "SQ":  "2026-05-01T00:00:00+00:00",
        "S":   "2026-05-02T00:00:00+00:00",
        "Q":   "2026-05-02T00:00:00+00:00",
        "R":   "2026-05-03T20:00:00+00:00",
    }
    # Saturday 18:00 UTC: post-Sprint but Q is later this UTC day.
    just_before_q = sessions_completed_by_calendar(
        schedule, reference_time="2026-05-02T18:00:00+00:00", buffer_minutes=90,
    )
    assert just_before_q == ["FP1", "SQ", "S"]
    # Sunday 00:30 UTC: end of Saturday UTC is past, Q now considered done.
    after_eod = sessions_completed_by_calendar(
        schedule, reference_time="2026-05-03T00:30:00+00:00", buffer_minutes=90,
    )
    assert after_eod == ["FP1", "SQ", "S", "Q"]


def test_sessions_completed_by_calendar_enforces_chronological_order() -> None:
    # Even if SQ has a precise timestamp that falls before FP1 (corrupt
    # upstream), we must not let SQ advance the target without FP1 also
    # being done. We sort by start time, so the natural chronology wins.
    schedule = {
        "Q":  "2026-05-02T00:00:00+00:00",
        "FP1": "2026-05-01T20:30:00+00:00",
        "SQ":  "2026-05-02T00:30:00+00:00",
    }
    # 60 min after SQ scheduled start: SQ session itself still ongoing,
    # so SQ is not done. FP1 finished long ago though.
    completed = sessions_completed_by_calendar(
        schedule, reference_time="2026-05-02T01:30:00+00:00", buffer_minutes=90,
    )
    assert completed == ["FP1"]


def test_sessions_completed_by_calendar_uses_country_offset_for_date_only() -> None:
    # Miami sprint Saturday: real Q ends ~21:00 UTC (16:00 EDT + 1h
    # session, EDT = UTC-4). With country="United States" + sprint
    # format, the helper must derive Q completion at 21:00 UTC instead
    # of falling back to end-of-day Sunday.
    schedule = {
        "FP1": "2026-05-01T00:00:00+00:00",
        "SQ":  "2026-05-01T00:00:00+00:00",
        "S":   "2026-05-02T00:00:00+00:00",
        "Q":   "2026-05-02T00:00:00+00:00",
        "R":   "2026-05-03T20:00:00+00:00",
    }
    # Saturday 21:43 UTC: real Q is over (~21:00 UTC). Target must
    # advance to race.
    completed = sessions_completed_by_calendar(
        schedule,
        reference_time="2026-05-02T21:43:00+00:00",
        country="United States",
        weekend_format="sprint",
    )
    assert completed == ["FP1", "SQ", "S", "Q"]
    # Saturday 20:30 UTC: Q hasn't started yet (~20:00 UTC start in
    # Miami). Target must still be qualifying.
    pre_q = sessions_completed_by_calendar(
        schedule,
        reference_time="2026-05-02T20:30:00+00:00",
        country="United States",
        weekend_format="sprint",
    )
    assert pre_q == ["FP1", "SQ", "S"]


def test_sessions_completed_by_calendar_falls_back_when_country_unknown() -> None:
    # Unknown / empty country -> position-of-day fallback, identical to
    # the pre-country behaviour (last-of-day +24h, earlier +18h). This
    # protects new race hosts that haven't been added to the offset map.
    schedule = {
        "S": "2026-05-02T00:00:00+00:00",
        "Q": "2026-05-02T00:00:00+00:00",
    }
    completed = sessions_completed_by_calendar(
        schedule,
        reference_time="2026-05-02T22:00:00+00:00",
        country="Atlantis",
        weekend_format="sprint",
    )
    assert completed == ["S"]


def test_sessions_completed_by_calendar_country_for_european_conventional() -> None:
    # Spain conventional Q on Saturday: real end ~15:00 UTC
    # (16:00 local CEST = UTC+2 + 1h session - 2h offset = 15:00 UTC).
    # We use SESSION_END_LOCAL_HOUR conventional Q = 17.5 -> 17.5 - 2.0
    # = 15.5h after midnight UTC = 15:30 UTC.
    schedule = {
        "FP3": "2026-06-13T00:00:00+00:00",
        "Q":   "2026-06-13T00:00:00+00:00",
    }
    pre_q = sessions_completed_by_calendar(
        schedule,
        reference_time="2026-06-13T15:00:00+00:00",
        country="Spain",
        weekend_format="conventional",
    )
    # FP3 typical end 13:30 local = 11:30 UTC -> threshold 11:30 UTC,
    # already past. Q threshold 15:30 UTC, not yet.
    assert pre_q == ["FP3"]
    post_q = sessions_completed_by_calendar(
        schedule,
        reference_time="2026-06-13T16:00:00+00:00",
        country="Spain",
        weekend_format="conventional",
    )
    assert post_q == ["FP3", "Q"]


def test_sessions_completed_by_calendar_full_day_advance() -> None:
    # The day after qualifying day, every date-only session is firmly in
    # the past and only the precise R timestamp + buffer gates further.
    schedule = {
        "FP1": "2026-05-01T00:00:00+00:00",
        "SQ":  "2026-05-01T00:00:00+00:00",
        "S":   "2026-05-02T00:00:00+00:00",
        "Q":   "2026-05-02T00:00:00+00:00",
        "R":   "2026-05-03T20:00:00+00:00",
    }
    completed = sessions_completed_by_calendar(
        schedule, reference_time="2026-05-03T05:00:00+00:00", buffer_minutes=90,
    )
    # Race start hasn't occurred yet (20:00 UTC).
    assert completed == ["FP1", "SQ", "S", "Q"]
