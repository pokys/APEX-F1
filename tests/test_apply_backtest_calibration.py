from __future__ import annotations

from pathlib import Path

from pipeline.apply_backtest_calibration import choose_backtest_file


def test_choose_backtest_file_prefers_requested_season(tmp_path: Path) -> None:
    root = tmp_path / "backtest"
    root.mkdir(parents=True)
    (root / "backtest_season_2024.json").write_text("{}", encoding="utf-8")
    (root / "backtest_season_2025.json").write_text("{}", encoding="utf-8")

    chosen = choose_backtest_file(root, season=2024)
    assert chosen is not None
    assert chosen.name == "backtest_season_2024.json"


def test_choose_backtest_file_falls_back_to_latest_season(tmp_path: Path) -> None:
    root = tmp_path / "backtest"
    root.mkdir(parents=True)
    (root / "backtest_season_2023.json").write_text("{}", encoding="utf-8")
    (root / "backtest_season_2025.json").write_text("{}", encoding="utf-8")

    chosen = choose_backtest_file(root, season=2026)
    assert chosen is not None
    assert chosen.name == "backtest_season_2025.json"


import argparse
import json

from pipeline.apply_backtest_calibration import apply_calibration


def _write_report(path: Path, races: int) -> None:
    path.write_text(json.dumps({"races_evaluated": races, "summary": {}}), encoding="utf-8")


def test_choose_backtest_file_needs_enough_races_in_current_season(tmp_path: Path) -> None:
    root = tmp_path / "backtest"
    root.mkdir()
    _write_report(root / "backtest_season_2025.json", 23)
    _write_report(root / "backtest_season_2026.json", 3)
    assert choose_backtest_file(root, season=2026).name == "backtest_season_2025.json"
    _write_report(root / "backtest_season_2026.json", 15)
    assert choose_backtest_file(root, season=2026).name == "backtest_season_2026.json"


def test_apply_calibration_prefers_noise_scales() -> None:
    args = argparse.Namespace(min_temp=0.6, max_temp=1.8, min_qualifying_temp=0.6, max_qualifying_temp=1.8)
    config = {"win_temperature": 1.2, "qualifying_temperature": 1.6}
    applied = apply_calibration(
        config,
        {
            "recommended_qualifying_noise_scale": 2.0,
            "recommended_race_noise_scale": 1.5,
            "recommended_standings_blend_qualifying": 0.5,
            "recommended_standings_blend_race": 0.5,
            "recommended_win_temperature": 1.2,
        },
        args,
    )
    assert applied == {"qualifying_noise_scale": 2.0, "race_noise_scale": 1.5, "standings_blend_qualifying": 0.5, "standings_blend_race": 0.5}
    assert "win_temperature" not in config and "qualifying_temperature" not in config

    legacy: dict = {}
    assert apply_calibration(legacy, {"recommended_win_temperature": 1.25}, args) == {"win_temperature": 1.25}
