from __future__ import annotations

import json
from pathlib import Path

from pipeline.build_features import (
    choose_fastf1_snapshot,
    aggregate_signals,
    build_features,
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
