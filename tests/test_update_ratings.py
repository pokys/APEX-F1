from __future__ import annotations

import json
from pathlib import Path

from pipeline.update_ratings import (
    choose_features_file,
    aggregate_optional_signal_indexes,
    DEFAULT_SIGNAL_GUARDRAILS,
)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_choose_features_file_falls_back_to_previous_season(tmp_path: Path) -> None:
    root = tmp_path / "processed"
    root.mkdir(parents=True)

    write_json(
        root / "features_season_2025.json",
        {"season": 2025, "drivers": [{"driver": "AAA", "team": "X"}], "teams": [{"team": "X"}]},
    )
    write_json(
        root / "features_season_2026.json",
        {"season": 2026, "drivers": [], "teams": []},
    )

    chosen = choose_features_file(root, season=2026)
    assert chosen.name == "features_season_2025.json"


def test_penalty_index_is_capped_by_guardrails() -> None:
    guardrails = json.loads(json.dumps(DEFAULT_SIGNAL_GUARDRAILS))
    signals = [
        {
            "team": "Aston Martin",
            "new_component_penalty": 0.95,
            "source_confidence": 0.9,
            "source_name": "autosport",
            "article_hash": "h1",
        }
    ]
    _, _, penalties = aggregate_optional_signal_indexes(signals, guardrails=guardrails)
    assert penalties["aston-martin"] <= guardrails["penalty_index_cap"]
