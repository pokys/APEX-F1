from __future__ import annotations

from datetime import date
from pathlib import Path

from pipeline.collect_articles import archived_hashes, make_article_hash, rotate_inbox


def test_rotate_inbox_archives_old_sections_with_checkbox_state(tmp_path: Path) -> None:
    lines = [
        "# F1 Article Inbox",
        "",
        "## 2026-09-01",
        "- [x] Old processed story (https://example.com/a)",
        "- [ ] Old open story (https://example.com/b)",
        "",
        "## 2026-10-05",
        "- [ ] Fresh story (https://example.com/c)",
        "",
    ]
    kept = rotate_inbox(lines, date(2026, 10, 8), 14, tmp_path)
    assert "## 2026-09-01" not in kept
    assert "- [ ] Fresh story (https://example.com/c)" in kept
    archived = (tmp_path / "articles_2026-09.md").read_text(encoding="utf-8")
    assert "- [x] Old processed story (https://example.com/a)" in archived
    assert make_article_hash("Old open story", "https://example.com/b") in archived_hashes(tmp_path)
    # Rotating again keeps the archive free of duplicates.
    assert rotate_inbox(kept, date(2026, 10, 8), 14, tmp_path) == kept


def test_full_pipeline_does_not_wipe_the_inbox() -> None:
    workflow = Path(".github/workflows/full-pipeline.yml").read_text(encoding="utf-8")
    assert "> knowledge/inbox/articles.md" not in workflow
