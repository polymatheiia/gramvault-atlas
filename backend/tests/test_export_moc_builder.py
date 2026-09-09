"""`gramvault.export.moc_builder` — per-category MOC note rendering (§G3)."""

from __future__ import annotations

from gramvault.export.index_builder import IndexEntry
from gramvault.export.markdown_builder import MANAGED_END, MANAGED_START
from gramvault.export.moc_builder import build_moc_markdown, moc_filename


def _entry(item_id: int, *, author: str = "acc", date: str = "2026-01-01") -> IndexEntry:
    return IndexEntry(
        item_id=item_id,
        note_filename=f"{date}_{author}_{item_id}.md",
        author=author,
        media_type="reel",
        date=date,
    )


class TestMocFilename:
    def test_sanitizes_and_adds_md(self) -> None:
        assert moc_filename("books/manga") == "books-manga.md"

    def test_stable(self) -> None:
        assert moc_filename("psychology") == moc_filename("psychology") == "psychology.md"


class TestBuildMoc:
    def test_has_frontmatter_managed_region_and_dataview(self) -> None:
        md = build_moc_markdown(
            "psychology", [_entry(1), _entry(2)], subfolder_name="GramVault"
        )
        assert "gramvault_moc: psychology" in md
        assert "count: 2" in md
        assert md.index(MANAGED_START) < md.index("```dataview") < md.index(MANAGED_END)
        assert 'FROM "GramVault"' in md
        assert 'WHERE category = "psychology"' in md

    def test_lists_items_newest_first_as_note_links(self) -> None:
        md = build_moc_markdown(
            "psychology",
            [_entry(1, date="2026-01-01"), _entry(2, date="2026-05-01")],
            subfolder_name="GramVault",
        )
        assert "[[2026-05-01_acc_2]]" in md
        assert md.index("_acc_2]]") < md.index("_acc_1]]")

    def test_embeds_digest_when_given(self) -> None:
        md = build_moc_markdown(
            "psychology",
            [_entry(1)],
            subfolder_name="GramVault",
            digest_markdown="## Themes\n- be kind [[2026-01-01_acc_1]]\n",
        )
        assert "## Latest digest" in md
        assert "be kind" in md
        assert md.index("## Latest digest") < md.index("## Items")

    def test_no_digest_section_without_one(self) -> None:
        md = build_moc_markdown("psychology", [_entry(1)], subfolder_name="GramVault")
        assert "## Latest digest" not in md

    def test_preserves_user_tail(self) -> None:
        first = build_moc_markdown("psychology", [_entry(1)], subfolder_name="GramVault")
        edited = first + "\n## My notes\n\nkeep this\n"
        second = build_moc_markdown(
            "psychology", [_entry(1), _entry(2)], subfolder_name="GramVault", existing_text=edited
        )
        assert "keep this" in second
        assert "count: 2" in second
