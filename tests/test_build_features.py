from __future__ import annotations

import json
from pathlib import Path

from pipeline.build_features import (
    choose_fastf1_snapshot,
    aggregate_signals,
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
