from __future__ import annotations

import json
from pathlib import Path

from pipeline.update_ratings import choose_features_file


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
