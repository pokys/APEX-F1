from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pipeline.render_prediction_page import track_record_html
from pipeline.track_record import archive_prediction, evaluate_archive

CONFIG = {
    "season": 2026,
    "next_round": 17,
    "race": "Singapore Grand Prix",
    "target_session_code": "SQ",
    "sessions_schedule": {"SQ": "2026-10-09T12:30:00+00:00"},
}
PREDICTION = {
    "race": "Singapore Grand Prix",
    "target_session_code": "SQ",
    "prediction_target": "sprint_qualifying",
    "drivers": [
        {"name": "RUS", "pole_probability": 0.6, "top10_probability": 1.0},
        {"name": "NOR", "pole_probability": 0.4, "top10_probability": 1.0},
    ],
}


def test_archive_is_written_before_and_frozen_after_session_start(tmp_path: Path) -> None:
    before = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    path = archive_prediction(PREDICTION, CONFIG, tmp_path, before)
    assert path is not None and path.name == "17_SQ.json"
    changed = dict(PREDICTION, drivers=[{"name": "NOR", "pole_probability": 1.0}])
    assert archive_prediction(changed, CONFIG, tmp_path, after) is None
    assert '"RUS"' in path.read_text(encoding="utf-8")


def test_archived_prediction_is_scored_once_results_exist(tmp_path: Path) -> None:
    import json

    archive_prediction(PREDICTION, CONFIG, tmp_path / "archive", datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc))
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "season_2026.json").write_text(
        json.dumps({"events": [{"round": 17, "sessions": [{"session_code": "SQ", "results": [
            {"abbreviation": "NOR", "position": 1},
            {"abbreviation": "RUS", "position": 2},
        ]}]}]}),
        encoding="utf-8",
    )
    record = evaluate_archive(tmp_path / "archive", raw)
    assert record["summary"]["count"] == 1
    entry = record["entries"][0]
    assert entry["actual_winner"] == "NOR" and entry["predicted_winner"] == "RUS" and entry["hit"] is False
    assert entry["winner_probability"] == 0.4
    assert "Track Record" in track_record_html(record)
