from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.backtest_simulation import fixed_grid_from_event
from pipeline.build_features import DEFAULT_SIGNAL_GUARDRAILS, build_features
from pipeline.import_penalties import collect_penalties, parse_penalty_message
from pipeline.openf1_client import OpenF1Client
from pipeline.prediction_targeting import apply_grid_penalties, grid_penalties_from_signals, signal_count
from pipeline.render_prediction_page import penalties_html
from pipeline.update_ratings import apply_roster_changes
from pipeline.validate_signals import validate_signal

ORDER = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]


def _penalty(driver, **kw):
    base = {
        "type": "grid_penalty",
        "season": 2026,
        "event": "Singapore Grand Prix",
        "driver": driver,
        "source_name": "fia",
        "source_url": "https://www.fia.com/documents/x",
        "timestamp": "2026-10-09T10:00:00+00:00",
    }
    base.update(kw)
    return base


def test_apply_grid_penalties_places_back_of_grid_and_pit_lane() -> None:
    penalties = [
        {"driver": "AAA", "places": 3},
        {"driver": "BBB", "back_of_grid": True},
        {"driver": "CCC", "pit_lane": True},
    ]
    assert apply_grid_penalties(ORDER, penalties) == ["DDD", "EEE", "FFF", "AAA", "BBB", "CCC"]
    assert apply_grid_penalties(ORDER, [{"driver": "EEE", "places": 10}]) == ["AAA", "BBB", "CCC", "DDD", "FFF", "EEE"]
    assert apply_grid_penalties(ORDER, [{"driver": "AAA", "excluded": True}]) == ORDER[1:]


def test_grid_penalties_from_signals_respects_session_and_sums_places() -> None:
    signals = [
        _penalty("AAA", places=3),
        _penalty("AAA", places=2),
        _penalty("BBB", pit_lane=True, applies_to="sprint"),
        {"type": "pu_element_change", "driver": "CCC", "back_of_grid": True, "source_url": "https://x"},
    ]
    race = {p["driver"]: p for p in grid_penalties_from_signals(signals, "race")}
    assert race["AAA"]["places"] == 5 and race["CCC"]["back_of_grid"] is True and "BBB" not in race
    sprint = grid_penalties_from_signals(signals, "sprint")
    assert [p["driver"] for p in sprint] == ["BBB"]


def test_event_signals_do_not_count_as_soft_signals(tmp_path: Path) -> None:
    (tmp_path / "penalties_2026.json").write_text(json.dumps({"signals": [_penalty("AAA", places=3)]}), encoding="utf-8")
    (tmp_path / "signals_2026-10-01.json").write_text(json.dumps({"signals": [{"team": "Ferrari", "source_name": "x", "article_hash": "h", "source_confidence": 0.5}]}), encoding="utf-8")
    assert signal_count(tmp_path) == 1


def test_validate_event_signals() -> None:
    assert validate_signal(_penalty("AAA", places=3), "f", 0) == []
    assert validate_signal(_penalty("AAA", places=3, pit_lane=True), "f", 0)
    assert validate_signal(_penalty("aaaa", places=3), "f", 0)
    assert validate_signal(dict(_penalty("AAA", places=3), source_url="fia.com"), "f", 0)
    sub = {
        "type": "driver_substitution",
        "season": 2026,
        "event": "Singapore Grand Prix",
        "driver_out": "STR",
        "driver_in": "DRU",
        "team": "Aston Martin",
        "source_name": "the-race",
        "source_url": "https://www.the-race.com/x",
        "timestamp": "2026-10-08T15:00:00+00:00",
    }
    assert validate_signal(sub, "f", 0) == []
    assert validate_signal({"type": "rumour", "source_name": "x"}, "f", 0)


def test_roster_changes_for_substitution_and_ban() -> None:
    roster = {"STR": "Aston Martin", "ALO": "Aston Martin", "VER": "Red Bull"}
    changed = apply_roster_changes(
        roster,
        [
            {"type": "driver_substitution", "driver_out": "STR", "driver_in": "DRU", "team": "Aston Martin F1"},
            {"type": "race_ban", "driver": "VER"},
        ],
    )
    assert changed == {"ALO": "Aston Martin", "DRU": "Aston Martin"}


def test_historical_grid_penalty_rate_from_grid_vs_qualifying() -> None:
    def q(pos, abbr):
        return {"position": pos, "abbreviation": abbr, "team_name": "T" + abbr}

    def r(pos, abbr, grid, status="Finished"):
        return {"position": pos, "abbreviation": abbr, "team_name": "T" + abbr, "grid_position": grid, "status": status}

    events = [
        {"event_date": "2026-03-01", "sessions": [
            {"session_code": "Q", "results": [q(1, "AAA"), q(2, "BBB"), q(3, "CCC")]},
            {"session_code": "R", "results": [r(1, "BBB", 1), r(2, "CCC", 2), r(3, "AAA", 0)]},
        ]},
        {"event_date": "2026-03-08", "sessions": [
            {"session_code": "Q", "results": [q(1, "AAA"), q(2, "BBB"), q(3, "CCC")]},
            {"session_code": "R", "results": [r(1, "AAA", 1), r(2, "BBB", 2), r(3, "CCC", 3, "Did not start")]},
        ]},
    ]
    features = build_features({"season": 2026, "events": events}, [], DEFAULT_SIGNAL_GUARDRAILS)
    drivers = {row["driver"]: row for row in features["drivers"]}
    assert drivers["AAA"]["grid_penalty_rate"] == 0.5
    assert drivers["BBB"]["grid_penalty_rate"] == 0.0
    # A did-not-start is not a grid observation.
    assert drivers["CCC"]["grid_penalty_rate"] == 0.0


def test_backtest_uses_actual_starting_grid() -> None:
    event = {"sessions": [
        {"session_code": "Q", "results": [{"position": 1, "abbreviation": "AAA"}, {"position": 2, "abbreviation": "BBB"}]},
        {"session_code": "R", "results": [
            {"position": 1, "abbreviation": "BBB", "grid_position": 1},
            {"position": 2, "abbreviation": "AAA", "grid_position": 0},
        ]},
    ]}
    assert fixed_grid_from_event(event) == ["BBB", "AAA"]


def test_known_penalty_moves_driver_back_on_simulated_grid() -> None:
    from pipeline.simulate_target_prediction import run_race_or_sprint_prediction

    def entry(name, rating):
        return {"name": name, "team": name, "driver_rating": rating, "team_rating": rating, "qualifying_rating": rating,
                "race_rating": rating, "qualifying_team_rating": rating, "race_team_rating": rating,
                "strategy_score": 50.0, "reliability_score": 99.0, "dnf_probability": 0.0}

    entries = [entry("AAA", 62.0), entry("BBB", 60.0), entry("CCC", 40.0)]
    base = {"seed": 3, "simulations": 5000, "overtaking_difficulty": 0.9, "track": {"qualifying_noise": 1.0, "race_noise": 2.0}}
    free = {r["name"]: r for r in run_race_or_sprint_prediction(entries, dict(base), {}, {}, {})["drivers"]}
    penalized = {r["name"]: r for r in run_race_or_sprint_prediction(entries, dict(base, grid_penalties=[{"driver": "AAA", "pit_lane": True}]), {}, {}, {})["drivers"]}
    assert penalized["AAA"]["win_probability"] < free["AAA"]["win_probability"] - 0.1


def test_parse_race_control_messages() -> None:
    assert parse_penalty_message("FIA STEWARDS: 3 PLACE GRID PENALTY FOR CAR 23 (ALB) - IMPEDING") == {
        "driver_number": 23, "driver": "ALB", "places": 3, "next_event": False}
    assert parse_penalty_message("CAR 31 (OCO) WILL START FROM THE PIT LANE")["pit_lane"] is True
    assert parse_penalty_message("FIA STEWARDS: 5 PLACE GRID DROP AT THE NEXT RACE FOR CAR 1 (VER)")["next_event"] is True
    assert parse_penalty_message("FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 1 (VER) - CAUSING A COLLISION") is None
    assert parse_penalty_message("CAR 23 (ALB) GRID PENALTY UNDER INVESTIGATION") is None


def test_collect_penalties_from_race_control_produces_valid_signals() -> None:
    sessions = [
        {"session_key": 1, "session_name": "Qualifying", "date_start": "2026-10-10T13:00:00+00:00"},
        {"session_key": 2, "session_name": "Sprint Qualifying", "date_start": "2026-10-09T12:30:00+00:00"},
        {"session_key": 3, "session_name": "Race", "date_start": "2026-10-04T07:00:00+00:00"},
    ]
    messages = {
        1: [{"date": "2026-10-10T13:40:00+00:00", "message": "FIA STEWARDS: 3 PLACE GRID PENALTY FOR CAR 23 (ALB) - IMPEDING"}],
        2: [{"date": "2026-10-09T13:10:00+00:00", "message": "FIA STEWARDS: 3 PLACE GRID PENALTY FOR CAR 4 (NOR) - IMPEDING"}],
        3: [
            {"date": "2026-10-04T09:00:00+00:00", "message": "FIA STEWARDS: 5 PLACE GRID PENALTY AT THE NEXT RACE FOR CAR 1 (VER)"},
            {"date": "2026-10-04T09:05:00+00:00", "message": "FIA STEWARDS: 3 PLACE GRID PENALTY FOR CAR 44 (HAM)"},
        ],
    }

    def fetch(endpoint, params):
        if endpoint == "sessions":
            return sessions
        if endpoint == "race_control":
            return messages.get(params["session_key"], [])
        return []

    calendar = [
        {"event_name": "Bahrain Grand Prix in Malaysia", "event_date": "2026-10-04", "sessions_schedule": {"R": "2026-10-04T07:00:00+00:00"}},
        {"event_name": "Singapore Grand Prix", "event_date": "2026-10-11", "sessions_schedule": {
            "SQ": "2026-10-09T12:30:00+00:00", "Q": "2026-10-10T13:00:00+00:00", "R": "2026-10-11T12:00:00+00:00"}},
    ]
    now = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    signals = collect_penalties(OpenF1Client(fetcher=fetch), 2026, calendar, "Singapore Grand Prix", now)
    found = {(s["driver"], s["applies_to"]) for s in signals}
    # HAM's penalty applied to the previous race itself, so it is not carried over.
    assert found == {("ALB", "race"), ("NOR", "sprint"), ("VER", "race")}
    assert all(validate_signal(s, "auto", i) == [] for i, s in enumerate(signals))
    assert all(s["event"] == "Singapore Grand Prix" for s in signals)


def test_dashboard_lists_penalties_with_source() -> None:
    page = penalties_html({"race_grid_penalties": [{"driver": "ALB", "places": 3, "sources": ["https://api.openf1.org/v1/race_control?session_key=1"]}]})
    assert "ALB" in page and "3 place grid drop" in page and "api.openf1.org" in page
    assert "No grid penalty" in penalties_html({})


def test_select_prediction_target_applies_penalties_to_qualifying_grid(tmp_path: Path, monkeypatch) -> None:
    import pipeline.select_prediction_target as module

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "season_2026.json").write_text(json.dumps({"events": [{
        "event_name": "Singapore Grand Prix",
        "event_format": "conventional",
        "sessions": [{"session_code": "Q", "results": [{"position": i + 1, "abbreviation": a} for i, a in enumerate(ORDER)]}],
    }], "calendar": []}), encoding="utf-8")
    signals = tmp_path / "signals"
    signals.mkdir()
    substitution = {
        "type": "driver_substitution", "season": 2026, "event": "Singapore Grand Prix", "driver_out": "FFF",
        "driver_in": "ZZZ", "team": "Team F", "source_name": "fia_decision_documents", "source_url": "https://www.fia.com/x.pdf",
        "timestamp": "2026-10-08T10:00:00",
    }
    (signals / "penalties_2026.json").write_text(json.dumps({"signals": [_penalty("AAA", places=3), substitution]}), encoding="utf-8")
    config = tmp_path / "race_config.json"
    config.write_text(json.dumps({"season": 2026, "race": "Singapore Grand Prix"}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "select_prediction_target", "--race-config", str(config), "--raw-dir", str(raw),
        "--calendar-cache-dir", str(tmp_path / "cal"), "--signals-dir", str(signals),
        "--session-weights", str(tmp_path / "none.json"), "--reference-time", "2026-10-10T20:00:00+00:00",
    ])
    assert module.main() == 0
    result = json.loads(config.read_text(encoding="utf-8"))
    assert result["prediction_target"] == "race"
    assert result["fixed_grid"] == ["BBB", "CCC", "DDD", "AAA", "EEE", "FFF"]
    assert result["grid_source"] == "qualifying+penalties"
    assert result["race_grid_penalties"][0]["driver"] == "AAA"
    assert result["driver_substitutions"] == [
        {"driver_in": "ZZZ", "driver_out": "FFF", "team": "Team F", "source": "https://www.fia.com/x.pdf"}
    ]
