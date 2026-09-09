"""Tests for gramvault.export.exporter: idempotent re-export, vault path
validation, media copy vs. link mode, and stale-note cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from gramvault.config import Config, ExportConfig, PathsConfig
from gramvault.export.exporter import (
    VaultNotConfiguredError,
    VaultPathNotFoundError,
    export_items,
)
from gramvault.export.markdown_builder import extract_gramvault_id, note_filename
from gramvault.models.schemas import Author, FileMediaType, Item, MediaFile, MediaType

# Notes the exporter always writes that aren't per-item.
_NON_ITEM_NOTES = {"GramVault Index.md", "GramVault Dashboard.md"}


def _config(tmp_path: Path, *, vault_dir: Path | None, media_mode: str = "copy") -> Config:
    return Config(
        paths=PathsConfig(
            library_dir=str(tmp_path / "library"),
            db_path=str(tmp_path / "data" / "gramvault.db"),
            chroma_dir=str(tmp_path / "data" / "chroma"),
            obsidian_vault_dir=str(vault_dir) if vault_dir is not None else None,
        ),
        export=ExportConfig(media_mode=media_mode),
    )


def _item(item_id: int = 1, **overrides) -> Item:
    defaults: dict = {
        "id": item_id,
        "author": Author(id=1, username="jane"),
        "media_type": MediaType.PHOTO,
        "caption": "A caption",
        "permalink": "https://instagram.com/p/xyz/",
        "media_files": [],
    }
    defaults.update(overrides)
    return Item(**defaults)


class TestVaultValidation:
    def test_no_vault_configured_raises(self, tmp_path: Path) -> None:
        config = _config(tmp_path, vault_dir=None)
        with pytest.raises(VaultNotConfiguredError):
            export_items(config, [_item()])

    def test_nonexistent_vault_path_raises_friendly_error(self, tmp_path: Path) -> None:
        missing_vault = tmp_path / "does-not-exist"
        config = _config(tmp_path, vault_dir=missing_vault)
        with pytest.raises(VaultPathNotFoundError) as exc_info:
            export_items(config, [_item()])
        assert str(missing_vault) in str(exc_info.value)

    def test_creates_target_subfolder_if_missing(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)
        result = export_items(config, [_item()])
        assert result.target_dir == vault_dir / "GramVault"
        assert result.target_dir.is_dir()

    def test_does_not_create_missing_base_vault_dir(self, tmp_path: Path) -> None:
        missing_vault = tmp_path / "no-such-vault"
        config = _config(tmp_path, vault_dir=missing_vault)
        with pytest.raises(VaultPathNotFoundError):
            export_items(config, [_item()])
        assert not missing_vault.exists()


class TestNoteWriting:
    def test_writes_one_note_per_item(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)
        items = [_item(1), _item(2)]
        result = export_items(config, items)
        assert result.notes_written == 2
        assert result.notes_updated == 0
        md_files = [p for p in result.target_dir.glob("*.md") if p.name not in _NON_ITEM_NOTES]
        assert len(md_files) == 2

    def test_skips_item_without_id(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)
        result = export_items(config, [_item(item_id=None)])
        assert result.notes_written == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].item_id is None

    def test_writes_index_note(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)
        result = export_items(config, [_item(1)])
        assert result.index_path is not None
        assert result.index_path.exists()
        assert result.index_path.name == "GramVault Index.md"


class TestIdempotentReexport:
    def test_rerun_does_not_duplicate_files(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)
        items = [_item(1), _item(2), _item(3)]

        first = export_items(config, items)
        md_files_after_first = {
            p.name for p in first.target_dir.glob("*.md") if p.name not in _NON_ITEM_NOTES
        }
        assert len(md_files_after_first) == 3
        assert first.notes_written == 3
        assert first.notes_updated == 0

        second = export_items(config, items)
        md_files_after_second = {
            p.name for p in second.target_dir.glob("*.md") if p.name not in _NON_ITEM_NOTES
        }
        # Same file set — no "Item (1).md"-style duplicates.
        assert md_files_after_second == md_files_after_first
        assert second.notes_written == 0
        assert second.notes_updated == 3

    def test_rerun_updates_changed_caption_in_place(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)

        export_items(config, [_item(1, caption="original caption")])
        filename = note_filename(_item(1))
        note_path = vault_dir / "GramVault" / filename
        assert "original caption" in note_path.read_text(encoding="utf-8")

        export_items(config, [_item(1, caption="updated caption")])
        assert note_path.exists()
        text = note_path.read_text(encoding="utf-8")
        assert "updated caption" in text
        assert "original caption" not in text

    def test_stale_note_removed_when_filename_changes(self, tmp_path: Path) -> None:
        # Simulate the author changing between exports, which changes the
        # deterministic filename — the old file for the same gramvault_id
        # should be cleaned up rather than left as an orphaned duplicate.
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)

        item_v1 = _item(1, author=Author(id=1, username="old-author"))
        export_items(config, [item_v1])
        old_filename = note_filename(item_v1)
        assert (vault_dir / "GramVault" / old_filename).exists()

        item_v2 = _item(1, author=Author(id=2, username="new-author"))
        result = export_items(config, [item_v2])
        new_filename = note_filename(item_v2)

        assert new_filename != old_filename
        assert not (vault_dir / "GramVault" / old_filename).exists()
        assert (vault_dir / "GramVault" / new_filename).exists()
        md_files = [p for p in result.target_dir.glob("*.md") if p.name not in _NON_ITEM_NOTES]
        assert len(md_files) == 1

    def test_gramvault_id_is_stable_lookup_key(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)
        export_items(config, [_item(1)])
        filename = note_filename(_item(1))
        note_text = (vault_dir / "GramVault" / filename).read_text(encoding="utf-8")
        assert extract_gramvault_id(note_text) == 1


class TestLayout:
    def _vault(self, tmp_path: Path) -> Path:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        return vault_dir

    def test_by_category_files_notes_under_category_folders(self, tmp_path: Path) -> None:
        vault_dir = self._vault(tmp_path)
        config = _config(tmp_path, vault_dir=vault_dir)
        config.export.layout = "by-category"

        export_items(config, [_item(1, category="beauty"), _item(2, category="workouts")])

        assert (vault_dir / "GramVault" / "beauty").is_dir()
        assert list((vault_dir / "GramVault" / "beauty").glob("*.md"))
        assert list((vault_dir / "GramVault" / "workouts").glob("*.md"))

    def test_changing_category_moves_the_note_and_keeps_user_tail(self, tmp_path: Path) -> None:
        vault_dir = self._vault(tmp_path)
        config = _config(tmp_path, vault_dir=vault_dir)
        config.export.layout = "by-category"

        export_items(config, [_item(1, category="beauty")])
        note = next((vault_dir / "GramVault" / "beauty").glob("*.md"))
        note.write_text(note.read_text() + "\n## Mine\n\nremember this\n", encoding="utf-8")

        export_items(config, [_item(1, category="skincare")])

        assert not list((vault_dir / "GramVault" / "beauty").glob("*.md"))
        moved = next((vault_dir / "GramVault" / "skincare").glob("*.md"))
        assert "remember this" in moved.read_text(encoding="utf-8")


class TestDashboard:
    def test_dashboard_note_written_with_category_counts(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)

        result = export_items(
            config, [_item(1, category="beauty"), _item(2, category="beauty"), _item(3)]
        )

        assert result.dashboard_path is not None and result.dashboard_path.exists()
        text = result.dashboard_path.read_text(encoding="utf-8")
        assert "| beauty | 2 |" in text
        assert "Uncategorized | 1" in text
        assert "%% gramvault:start %%" in text


class TestExportDigest:
    def test_writes_digest_note_and_rewrites_citations(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)
        from gramvault.export.exporter import export_digest

        item = _item(7, author=Author(id=1, username="booksacc"))
        path = export_digest(
            config,
            digest_id=1,
            name="My Reading List",
            markdown="## Sci-fi\n- **Dune** [[item:7]]\n- **Ghost** [[item:999]]\n",
            items=[item],
            template="book-titles",
        )

        assert path == vault_dir / "GramVault" / "_digests" / "My-Reading-List.md"
        text = path.read_text(encoding="utf-8")
        assert f"[[{note_filename(item).removesuffix('.md')}]]" in text
        assert "[[item:7]]" not in text
        assert "[[item:999]]" in text  # unknown id left as-is
        assert "gramvault_digest: 1" in text

    def test_digest_note_preserves_user_tail(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir)
        from gramvault.export.exporter import export_digest

        export_digest(config, digest_id=1, name="d", markdown="# v1\n", items=[])
        note = vault_dir / "GramVault" / "_digests" / "d.md"
        note.write_text(note.read_text() + "\n## Notes\n\nkeep me\n", encoding="utf-8")

        export_digest(config, digest_id=1, name="d", markdown="# v2\n", items=[])
        text = note.read_text(encoding="utf-8")
        assert "v2" in text and "v1" not in text
        assert "keep me" in text


class TestMediaModes:
    def test_copy_mode_copies_file_and_embeds(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        source_media = tmp_path / "source.jpg"
        source_media.write_bytes(b"fake jpg bytes")
        config = _config(tmp_path, vault_dir=vault_dir, media_mode="copy")

        item = _item(
            1,
            media_files=[
                MediaFile(
                    id=1, item_id=1, file_path=str(source_media), media_type=FileMediaType.PHOTO
                )
            ],
        )
        result = export_items(config, [item])
        assert result.media_files_copied == 1
        copied_files = list((vault_dir / "GramVault" / "media").glob("*.jpg"))
        assert len(copied_files) == 1

        note_text = (vault_dir / "GramVault" / note_filename(item)).read_text(encoding="utf-8")
        assert "![[media/" in note_text

    def test_copy_mode_video_gets_a_poster_frame(self, tmp_path: Path, monkeypatch) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        source_media = tmp_path / "clip.mp4"
        source_media.write_bytes(b"fake mp4 bytes")
        config = _config(tmp_path, vault_dir=vault_dir, media_mode="copy")

        calls: list[Path] = []

        def fake_poster(src: Path, dest: Path) -> bool:
            calls.append(Path(src))
            Path(dest).write_bytes(b"\xff\xd8 fake jpg")
            return True

        monkeypatch.setattr("gramvault.export.exporter.poster_frame", fake_poster)

        item = _item(
            1,
            media_files=[
                MediaFile(
                    id=1, item_id=1, file_path=str(source_media), media_type=FileMediaType.VIDEO
                )
            ],
        )
        export_items(config, [item])

        assert calls == [source_media]
        poster = vault_dir / "GramVault" / "media" / "1_0.poster.jpg"
        assert poster.is_file()
        note_text = (vault_dir / "GramVault" / note_filename(item)).read_text(encoding="utf-8")
        assert "![[media/1_0.poster.jpg]]" in note_text
        assert "[▶ video](media/1_0.mp4)" in note_text

        # Re-export: the poster already exists and is newer than the source,
        # so ffmpeg is not run again.
        calls.clear()
        export_items(config, [item])
        assert calls == []

    def test_by_category_layout_resolves_library_relative_media(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir, media_mode="copy")
        # A row whose file_path is stored relative to the library dir.
        lib_media = config.resolved_library_dir / "media" / "aa" / "pic.jpg"
        lib_media.parent.mkdir(parents=True)
        lib_media.write_bytes(b"fake jpg bytes")

        item = _item(
            1,
            media_files=[
                MediaFile(
                    id=1, item_id=1, file_path="media/aa/pic.jpg", media_type=FileMediaType.PHOTO
                )
            ],
        )
        result = export_items(config, [item])
        assert result.media_files_copied == 1
        assert list((vault_dir / "GramVault" / "media").glob("*.jpg"))

    def test_link_mode_does_not_copy_and_links_original(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        source_media = tmp_path / "source.jpg"
        source_media.write_bytes(b"fake jpg bytes")
        config = _config(tmp_path, vault_dir=vault_dir, media_mode="link")

        item = _item(
            1,
            media_files=[
                MediaFile(
                    id=1, item_id=1, file_path=str(source_media), media_type=FileMediaType.PHOTO
                )
            ],
        )
        result = export_items(config, [item])
        assert result.media_files_copied == 0
        assert not (vault_dir / "GramVault" / "media").exists()

        note_text = (vault_dir / "GramVault" / note_filename(item)).read_text(encoding="utf-8")
        assert "![[" not in note_text
        assert str(source_media) in note_text

    def test_copy_mode_missing_source_file_falls_back_to_link(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        config = _config(tmp_path, vault_dir=vault_dir, media_mode="copy")

        item = _item(
            1,
            media_files=[
                MediaFile(
                    id=1,
                    item_id=1,
                    file_path=str(tmp_path / "missing.jpg"),
                    media_type=FileMediaType.PHOTO,
                )
            ],
        )
        result = export_items(config, [item])
        assert result.media_files_copied == 0
        # Should not raise, and the note should still be written.
        assert result.notes_written == 1
