from __future__ import annotations

from datetime import datetime, timezone

from pipeline.collect_weather import build_forecast, session_forecast
from pipeline.prediction_history import phase_changes, record_phase
from pipeline.render_prediction_page import odds_bar_html, render_page, timeline_html

CONFIG = {
    "season": 2026,
    "next_round": 17,
    "race": "Singapore Grand Prix",
    "prediction_target": "qualifying",
    "target_session_code": "Q",
    "weekend_format": "sprint",
    "available_sessions": ["FP1", "SQ", "S"],
    "sessions_schedule": {
        "FP1": "2026-10-09T08:30:00+00:00",
        "SQ": "2026-10-09T12:30:00+00:00",
        "S": "2026-10-10T09:00:00+00:00",
        "Q": "2026-10-10T13:00:00+00:00",
        "R": "2026-10-11T12:00:00+00:00",
    },
}


def _prediction(probs: dict[str, float], target: str = "qualifying") -> dict:
    key = "pole_probability" if target == "qualifying" else "win_probability"
    drivers = []
    for name, p in probs.items():
        row = {"name": name, "team": "Mercedes", key: p, "expected_position": 2.0, "expected_finish": 2.0}
        if target == "qualifying":
            row.update({"front_row_probability": min(1.0, p * 2), "top10_probability": 0.95})
        else:
            row["podium_probability"] = min(1.0, p * 2)
        drivers.append(row)
    return {"race": "Singapore Grand Prix", "prediction_target": target, "target_session_code": "Q", "drivers": drivers}


# --- point 1: probability bars -------------------------------------------

def test_odds_bar_nests_headline_inside_wider_outcomes() -> None:
    row = {"team": "Ferrari", "headline_probability": 0.2, "secondary_probability": 0.45, "third_probability": 0.97}
    bar = odds_bar_html(row, "qualifying")
    assert 'class="odds-l1" style="width:20.00%"' in bar
    assert 'class="odds-l2" style="width:45.00%"' in bar
    assert 'class="odds-l3" style="width:97.00%"' in bar
    assert "Pole 20.0%" in bar and "Top 10 97.0%" in bar
    race = odds_bar_html({"team": "Ferrari", "headline_probability": 0.1, "secondary_probability": 0.4, "third_probability": 0.0}, "race")
    assert "odds-l3" not in race and "Podium 40.0%" in race


# --- point 2: timeline with local times and countdown --------------------

def test_timeline_lists_sessions_chronologically_with_countdown_and_rain() -> None:
    weather = {"race": "Singapore Grand Prix", "sessions": {"Q": {"rain_probability": 0.62, "precipitation_mm": 2.4}, "R": {"rain_probability": 0.96, "precipitation_mm": 0.2}}}
    html_out = timeline_html("sprint", ["FP1", "SQ", "S"], "Q", CONFIG["sessions_schedule"], weather)
    grid = html_out[html_out.index('class="timeline-grid"'):]
    order = [grid.index(f"<p>{name}</p>") for name in ("Practice 1", "Sprint Qualifying", "Sprint", "Qualifying", "Race")]
    assert order == sorted(order)
    assert 'data-countdown="2026-10-10T13:00:00+00:00"' in html_out
    assert 'data-local-time="2026-10-09T08:30:00+00:00"' in html_out
    assert "Rain 62% · 2.4 mm" in html_out
    # Only qualifying is flagged as a wet session; the race's 96 % is drizzle.
    assert html_out.count("rain-high") == 1
    assert "predicting now" in html_out


# --- point 3: changes against the phase before the last session ----------

def test_history_keeps_one_entry_per_phase_and_compares_with_previous_phase() -> None:
    history: dict = {}
    config = dict(CONFIG, available_sessions_ingested=["FP1"])
    record_phase(history, config, _prediction({"RUS": 0.30, "NOR": 0.20}), None)
    # Hourly recomputes inside the same phase overwrite it ...
    record_phase(history, config, _prediction({"RUS": 0.31, "NOR": 0.19}), None)
    assert len(history["events"]["2026-17"]["phases"]) == 1
    assert phase_changes(history, config) is None
    # ... a new session starts a new phase, compared with the state before it.
    config2 = dict(CONFIG, available_sessions_ingested=["FP1", "SQ"])
    record_phase(history, config2, _prediction({"RUS": 0.25, "NOR": 0.33}), None)
    changes = phase_changes(history, config2)
    assert changes["label"] == "before SQ"
    assert abs(changes["deltas"]["NOR"] - 0.14) < 1e-9
    assert abs(changes["deltas"]["RUS"] + 0.06) < 1e-9
    # Further recomputes in the new phase still compare with the pre-SQ state.
    record_phase(history, config2, _prediction({"RUS": 0.26, "NOR": 0.33}), None)
    assert phase_changes(history, config2)["label"] == "before SQ"


def test_new_prediction_target_has_no_change_reference() -> None:
    history: dict = {}
    record_phase(history, dict(CONFIG, available_sessions_ingested=["FP1"]), _prediction({"RUS": 0.3}), None)
    race_config = dict(CONFIG, available_sessions_ingested=["FP1", "SQ", "S", "Q"])
    record_phase(history, race_config, _prediction({"RUS": 0.4}, target="race"), None)
    assert phase_changes(history, race_config) is None


def test_history_keeps_only_recent_events() -> None:
    history: dict = {}
    for rnd in range(1, 8):
        record_phase(history, dict(CONFIG, next_round=rnd, available_sessions_ingested=[]), _prediction({"RUS": 0.3}), None)
    assert sorted(history["events"]) == ["2026-04", "2026-05", "2026-06", "2026-07"]


# --- point 4: rain forecast and recommended scenario ----------------------

def test_session_forecast_uses_worst_hour_of_the_session_window() -> None:
    series = [
        (datetime(2026, 10, 10, h, tzinfo=timezone.utc), p, a)
        for h, p, a in [(12, 20, 0.0), (13, 70, 1.2), (14, 40, 0.3), (15, 90, 5.0)]
    ]
    forecast = session_forecast(series, datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc), 60)
    assert forecast == {"rain_probability": 0.7, "precipitation_mm": 1.2}


def test_build_forecast_queries_open_meteo_for_the_circuit() -> None:
    calls = []

    def fetch(url, params):
        calls.append(params)
        times = [f"2026-10-{d:02d}T{h:02d}:00" for d in (9, 10, 11) for h in range(24)]
        return {"hourly": {"time": times, "precipitation_probability": [55] * len(times), "precipitation": [0.6] * len(times)}}

    circuits = {"by_event_name": {"Singapore Grand Prix": {"lat": 1.2914, "lon": 103.864}}}
    forecast = build_forecast(CONFIG, circuits, fetch, datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc))
    assert calls[0]["latitude"] == 1.2914 and calls[0]["start_date"] == "2026-10-09"
    assert forecast["sessions"]["Q"]["rain_probability"] == 0.55
    assert build_forecast(dict(CONFIG, race="Unknown GP"), circuits, fetch, datetime(2026, 10, 8, tzinfo=timezone.utc)) is None


def test_page_mixes_dry_and_wet_by_chance_of_a_wet_session() -> None:
    history: dict = {}
    record_phase(history, dict(CONFIG, available_sessions_ingested=["FP1", "SQ"]), _prediction({"RUS": 0.30, "NOR": 0.20}), _prediction({"RUS": 0.2, "NOR": 0.3}))
    record_phase(history, dict(CONFIG, available_sessions_ingested=["FP1", "SQ", "S"]), _prediction({"RUS": 0.22, "NOR": 0.35}), _prediction({"RUS": 0.2, "NOR": 0.3}))
    dry = _prediction({"RUS": 0.22, "NOR": 0.35})
    wet = _prediction({"RUS": 0.2, "NOR": 0.3})
    weather = {"race": "Singapore Grand Prix", "sessions": {"Q": {"rain_probability": 0.7, "precipitation_mm": 3.0}}}

    page = render_page(dry, CONFIG, prediction_wet=wet, weather=weather, history=history)
    assert 'class="scenario-panel is-active" data-scenario="mixed"' in page
    assert 'class="toggle-btn is-active" data-target="mixed"' in page
    assert 'data-target="dry"' in page and 'data-target="wet"' in page  # the toggle stays
    assert "Forecast mix (30% dry / 70% wet)" in page
    assert "31.5%" in page  # NOR: 0.3 * 35 % + 0.7 * 30 %
    assert "Rain risk for Qualifying: <strong>70%</strong>, 3.0 mm expected" in page
    # Changes mix too: dry NOR +15 pp, wet 0 pp -> +4.5 pp.
    assert "&#9650; 4.5" in page and "&#9650; 15.0" in page
    assert "change before S" in page

    soaked = render_page(dry, CONFIG, prediction_wet=wet, weather={"race": "Singapore Grand Prix", "sessions": {"Q": {"rain_probability": 1.0, "precipitation_mm": 5.0}}})
    assert 'class="scenario-panel is-active" data-scenario="wet"' in soaked and 'data-scenario="mixed"' not in soaked
    dry_page = render_page(dry, CONFIG, prediction_wet=wet, weather={"race": "Singapore Grand Prix", "sessions": {"Q": {"rain_probability": 0.02}}})
    assert 'class="scenario-panel is-active" data-scenario="dry"' in dry_page and 'data-scenario="mixed"' not in dry_page
    other_gp = render_page(dry, CONFIG, prediction_wet=wet, weather=dict(weather, race="Japanese Grand Prix"))
    assert 'class="scenario-panel is-active" data-scenario="dry"' in other_gp


def test_drizzle_counts_only_partly_as_wet() -> None:
    from pipeline.collect_weather import wet_session_probability

    assert wet_session_probability({"rain_probability": 0.96, "precipitation_mm": 0.2}) == 0.192
    assert wet_session_probability({"rain_probability": 0.7, "precipitation_mm": 3.0}) == 0.7
    assert wet_session_probability({"rain_probability": 0.4}) == 0.4
    assert wet_session_probability(None) is None


def test_blend_keeps_a_consistent_distribution() -> None:
    from pipeline.render_prediction_page import blend_predictions

    mixed = blend_predictions(_prediction({"RUS": 0.6, "NOR": 0.4}), _prediction({"RUS": 0.2, "NOR": 0.8}), 0.25)
    probs = {row["name"]: row["pole_probability"] for row in mixed["drivers"]}
    assert abs(probs["RUS"] - 0.5) < 1e-9 and abs(sum(probs.values()) - 1.0) < 1e-9


def test_penalty_badges_next_to_driver() -> None:
    from pipeline.render_prediction_page import penalty_badges, scenario_panel_html

    config = {
        "race_grid_penalties": [
            {"driver": "ALO", "places": 25, "back_of_grid": False, "pit_lane": False},
            {"driver": "LAW", "places": 0, "pit_lane": True},
        ],
        "sprint_grid_penalties": [{"driver": "ALO", "places": 3}],
    }
    badges = penalty_badges(config)
    assert "&minus;25 grid" in badges["ALO"] and "Sprint &minus;3 grid" in badges["ALO"]
    assert "pit start" in badges["LAW"]
    prediction = {
        "prediction_target": "race",
        "drivers": [
            {"name": "ALO", "team": "Aston Martin", "win_probability": 0.1, "podium_probability": 0.2, "expected_finish": 5.0},
            {"name": "VER", "team": "Red Bull Racing", "win_probability": 0.2, "podium_probability": 0.4, "expected_finish": 3.0},
        ],
    }
    page = scenario_panel_html(prediction, "dry", "Dry", True, None, badges)
    assert page.count("penalty-badge") == 3 * 2  # hero card, table row, mobile card; two badges for ALO
    assert "VER</strong><small>" in page


def test_substitute_badge() -> None:
    from pipeline.render_prediction_page import penalty_badges

    badges = penalty_badges({"driver_substitutions": [{"driver_in": "DRU", "driver_out": "STR", "team": "Aston Martin"}]})
    assert "sub for STR" in badges["DRU"]
