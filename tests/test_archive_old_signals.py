from __future__ import annotations

from datetime import date
from pathlib import Path

from pipeline.archive_old_signals import archive_signals, latest_completed_race_date


def test_latest_completed_race_date() -> None:
    calendar = [date(2026, 3, 8), date(2026, 3, 22), date(2026, 4, 5)]
    assert latest_completed_race_date(calendar, as_of=date(2026, 3, 3)) is None
    assert latest_completed_race_date(calendar, as_of=date(2026, 3, 10)) == date(2026, 3, 8)
    assert latest_completed_race_date(calendar, as_of=date(2026, 3, 30)) == date(2026, 3, 22)


def test_archive_signals_moves_only_files_up_to_cutoff(tmp_path: Path) -> None:
    signals_dir = tmp_path / "processed"
    archive_dir = signals_dir / "archive"
    signals_dir.mkdir(parents=True)

    (signals_dir / "signals_2026-03-03.json").write_text('{"signals":[]}\n', encoding="utf-8")
    (signals_dir / "signals_2026-03-09.json").write_text('{"signals":[]}\n', encoding="utf-8")
    (signals_dir / "signals_2026-03-25.json").write_text('{"signals":[]}\n', encoding="utf-8")
    (signals_dir / "README.md").write_text("ignore\n", encoding="utf-8")

    scanned, archived = archive_signals(signals_dir, archive_dir, cutoff_date=date(2026, 3, 10))
    assert scanned == 3
    assert archived == 2

    assert (archive_dir / "signals_2026-03-03.json").exists()
    assert (archive_dir / "signals_2026-03-09.json").exists()
    assert (signals_dir / "signals_2026-03-25.json").exists()
