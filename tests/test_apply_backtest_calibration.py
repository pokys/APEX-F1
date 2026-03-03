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
