from __future__ import annotations

from pipeline.validate_signals import validate_signal


def test_validate_signal_accepts_valid_record() -> None:
    signal = {
        "team": "Ferrari",
        "upgrade_detected": True,
        "upgrade_magnitude": "medium",
        "reliability_concern": 0.3,
        "driver_confidence_change": 0.2,
        "source_confidence": 0.9,
        "source_name": "autosport",
        "article_hash": "abc123",
        "extraction_version": "v1",
    }
    errors = validate_signal(signal, "signals_2026-03-01.json", 1)
    assert errors == []


def test_validate_signal_rejects_invalid_ranges_and_required_fields() -> None:
    signal = {
        "upgrade_detected": True,
        "upgrade_magnitude": "extreme",
        "source_confidence": 1.2,
        "reliability_concern": 2.0,
        "driver_confidence_change": -2.0,
    }
    errors = validate_signal(signal, "signals_2026-03-01.json", 1)
    assert any("source_name" in e for e in errors)
    assert any("article_hash" in e for e in errors)
    assert any("source_confidence" in e for e in errors)
    assert any("upgrade_magnitude" in e for e in errors)
    assert any("reliability_concern" in e for e in errors)
    assert any("driver_confidence_change" in e for e in errors)
