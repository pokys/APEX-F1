from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.validate_backtest_quality import load_json, validate_backtest_quality


def test_committed_backtest_report_passes_quality_gate() -> None:
    report = load_json(Path("outputs/backtest/backtest_season_2025.json"))
    gates = load_json(Path("config/backtest_quality_gates.json"))
    validate_backtest_quality(report, gates)


def test_validate_backtest_quality_rejects_bad_summary_max() -> None:
    report = {
        "races_evaluated": 23,
        "qualifying_sessions_evaluated": 23,
        "summary": {
            "mean_winner_log_loss": 2.0,
            "winner_accuracy": 0.4,
        },
    }
    gates = {
        "minimum_counts": {"races_evaluated": 20},
        "summary_max": {"mean_winner_log_loss": 1.75},
        "summary_min": {"winner_accuracy": 0.25},
    }

    with pytest.raises(ValueError, match="mean_winner_log_loss above quality gate"):
        validate_backtest_quality(report, gates)


def test_validate_backtest_quality_rejects_low_sample_count() -> None:
    report = {
        "races_evaluated": 5,
        "summary": {
            "mean_winner_log_loss": 1.0,
        },
    }
    gates = {
        "minimum_counts": {"races_evaluated": 20},
        "summary_max": {"mean_winner_log_loss": 1.75},
    }

    with pytest.raises(ValueError, match="races_evaluated below quality gate"):
        validate_backtest_quality(report, gates)
