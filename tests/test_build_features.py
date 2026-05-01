from __future__ import annotations

import json
from pathlib import Path

from pipeline.build_features import (
    choose_fastf1_snapshot,
    aggregate_signals,
    build_features,
    load_recency_config,
    recency_weighted_mean,
    DEFAULT_RECENCY_CONFIG,
    DEFAULT_SIGNAL_GUARDRAILS,
)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_choose_fastf1_snapshot_falls_back_to_previous_season(tmp_path: Path) -> None:
    root = tmp_path / "fastf1"
    root.mkdir(parents=True)

    write_json(
        root / "season_2025.json",
        {
            "season": 2025,
            "events": [{"sessions": [{"results": [{"position": 1, "abbreviation": "AAA"}]}]}],
        },
    )
    write_json(
        root / "season_2026.json",
        {"season": 2026, "events": []},
    )

    chosen = choose_fastf1_snapshot(root, season=2026)
    assert chosen.name == "season_2025.json"


def test_aggregate_signals_applies_echo_decay_and_caps() -> None:
    guardrails = json.loads(json.dumps(DEFAULT_SIGNAL_GUARDRAILS))
    signals = [
        {
            "team": "Aston Martin",
            "reliability_concern": 0.9,
            "source_confidence": 0.9,
            "source_name": "autosport",
            "article_hash": "a1",
        },
        {
            "team": "Aston Martin",
            "reliability_concern": 0.9,
            "source_confidence": 0.9,
            "source_name": "motorsport",
            "article_hash": "a2",
        },
    ]

    team_agg, _ = aggregate_signals(signals, guardrails=guardrails)
    team = team_agg["aston-martin"]
    # Echo decay is applied, so the second same-claim signal contributes less.
    weighted_avg = team["reliability_weighted_sum"] / team["weight_sum"]
    assert weighted_avg <= 0.9


def test_build_features_collects_session_specific_metrics() -> None:
    snapshot = {
        "season": 2026,
        "events": [
            {
                "sessions": [
                    {"session_code": "FP1", "results": [{"position": 3, "abbreviation": "RUS", "team_name": "Mercedes"}]},
                    {"session_code": "FP2", "results": [{"position": 2, "abbreviation": "RUS", "team_name": "Mercedes"}]},
                    {"session_code": "FP3", "results": [{"position": 1, "abbreviation": "RUS", "team_name": "Mercedes"}]},
                    {"session_code": "SQ", "results": [{"position": 2, "abbreviation": "RUS", "team_name": "Mercedes", "q1": "1:30", "q2": "1:29"}]},
                    {"session_code": "S", "results": [{"position": 4, "abbreviation": "RUS", "team_name": "Mercedes"}]},
                    {"session_code": "Q", "results": [{"position": 1, "abbreviation": "RUS", "team_name": "Mercedes", "q1": "1:30", "q2": "1:29", "q3": "1:28"}]},
                    {"session_code": "R", "results": [{"position": 2, "abbreviation": "RUS", "team_name": "Mercedes", "status": "Finished", "points": 18}]},
                ]
            }
        ],
    }

    features = build_features(snapshot, [], DEFAULT_SIGNAL_GUARDRAILS)
    driver = features["drivers"][0]
    team = features["teams"][0]

    assert driver["practice_avg_position"] == 2.0
    assert driver["sprint_qualifying_avg_position"] == 2.0
    assert driver["sprint_avg_position"] == 4.0
    assert driver["qualifying_phase_depth"] == 1.0
    assert round(driver["sprint_qualifying_phase_depth"], 6) == round(2 / 3, 6)
    assert team["practice_avg_position"] == 2.0


def test_recency_weighted_mean_assigns_decay_per_event_rank() -> None:
    # Three events: oldest=10, middle=5, newest=2. Half-life of one event
    # halves the weight per step, so the weighted mean must be much closer
    # to the newest value than the simple arithmetic mean of (10+5+2)/3 = 5.67.
    pairs = [(0, 10.0), (1, 5.0), (2, 2.0)]
    mean, ess = recency_weighted_mean(pairs, half_life=1.0)
    expected = (1 * 2.0 + 0.5 * 5.0 + 0.25 * 10.0) / (1 + 0.5 + 0.25)
    assert round(mean, 6) == round(expected, 6)
    assert mean < (10.0 + 5.0 + 2.0) / 3
    # Kish ESS = (1 + 0.5 + 0.25)^2 / (1 + 0.25 + 0.0625) = 3.0625/1.3125 ~ 2.333.
    assert round(ess, 4) == 2.3333


def test_recency_weighted_mean_handles_empty_input() -> None:
    mean, ess = recency_weighted_mean([], half_life=4.0)
    assert mean is None
    assert ess == 0.0


def test_recency_weighted_mean_uniform_for_single_event() -> None:
    # Sessions inside the same event share an event_idx so all weights = 1.
    pairs = [(7, 1.0), (7, 2.0), (7, 3.0)]
    mean, ess = recency_weighted_mean(pairs, half_life=4.0)
    assert mean == 2.0
    assert round(ess, 6) == 3.0


def test_build_features_recency_biases_recent_race_over_old() -> None:
    # Driver finished P15 in event 0 and P3 in event 1 (latest).
    # With half_life=1.0 race the weighted mean should clearly favor P3.
    snapshot = {
        "season": 2026,
        "events": [
            {
                "event_date": "2026-03-08",
                "sessions": [
                    {"session_code": "R", "results": [{"position": 15, "abbreviation": "VER", "team_name": "Red Bull", "status": "Finished"}]},
                ],
            },
            {
                "event_date": "2026-03-29",
                "sessions": [
                    {"session_code": "R", "results": [{"position": 3, "abbreviation": "VER", "team_name": "Red Bull", "status": "Finished"}]},
                ],
            },
        ],
    }

    recency = json.loads(json.dumps(DEFAULT_RECENCY_CONFIG))
    recency["half_life_events"]["race"] = 1.0
    features = build_features(snapshot, [], DEFAULT_SIGNAL_GUARDRAILS, recency_config=recency)
    driver = features["drivers"][0]

    # Plain mean would be (15+3)/2 = 9. With weight ratio 1.0 (newest) vs
    # 0.5 (older), the weighted mean is (1*3 + 0.5*15) / 1.5 = 7.0.
    assert driver["race_avg_position"] == 7.0
    # ESS for two events at half-life=1: (1+0.5)^2/(1+0.25) = 1.8.
    assert round(driver["race_effective_starts"], 6) == 1.8
    assert driver["starts"] == 2  # raw count is preserved


def test_build_features_emits_default_recency_metadata() -> None:
    snapshot = {
        "season": 2026,
        "events": [
            {
                "event_date": "2026-03-08",
                "sessions": [
                    {"session_code": "R", "results": [{"position": 1, "abbreviation": "VER", "team_name": "Red Bull", "status": "Finished"}]},
                ],
            }
        ],
    }
    features = build_features(snapshot, [], DEFAULT_SIGNAL_GUARDRAILS)
    assert isinstance(features["recency_config"]["half_life_events"], dict)
    assert features["recency_config"]["half_life_events"].get("race") == DEFAULT_RECENCY_CONFIG["half_life_events"]["race"]


def test_load_recency_config_falls_back_when_missing(tmp_path: Path) -> None:
    cfg = load_recency_config(tmp_path / "missing.json")
    assert cfg["half_life_events"]["race"] == DEFAULT_RECENCY_CONFIG["half_life_events"]["race"]


def test_load_recency_config_overrides_only_valid_keys(tmp_path: Path) -> None:
    path = tmp_path / "recency.json"
    path.write_text(
        json.dumps(
            {
                "half_life_events": {"race": 6.0, "qualifying": -1, "bogus": 9},
                "stale_threshold_days": 14,
            }
        ),
        encoding="utf-8",
    )
    cfg = load_recency_config(path)
    assert cfg["half_life_events"]["race"] == 6.0
    # negative ignored, default kept
    assert cfg["half_life_events"]["qualifying"] == DEFAULT_RECENCY_CONFIG["half_life_events"]["qualifying"]
    # arbitrary keys are tolerated
    assert cfg["half_life_events"]["bogus"] == 9.0
    assert cfg["stale_threshold_days"] == 14.0
