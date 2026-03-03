from __future__ import annotations

import json
from pathlib import Path

from pipeline.build_features import choose_fastf1_snapshot


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
