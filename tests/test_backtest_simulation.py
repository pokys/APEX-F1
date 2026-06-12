from __future__ import annotations

from pipeline.backtest_simulation import event_chronology_key


def test_backtest_orders_cancelled_or_skipped_rounds_by_event_date() -> None:
    events = [
        {"round": 5, "event_name": "Later GP", "event_date": "2026-05-24"},
        {"round": 6, "event_name": "Earlier GP", "event_date": "2026-05-03"},
    ]

    ordered = sorted(events, key=event_chronology_key)

    assert [event["event_name"] for event in ordered] == ["Earlier GP", "Later GP"]
