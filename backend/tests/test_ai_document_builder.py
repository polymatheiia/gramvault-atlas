"""`gramvault.ai.document_builder.build_content_document` — the merged text
that gets embedded and shown as a chat citation."""

from __future__ import annotations

from gramvault.ai.document_builder import build_content_document
from gramvault.models.schemas import Item, MediaFile


def _item(**media_kwargs: object) -> Item:
    return Item(
        id=1,
        media_type="video",
        caption="a short caption",
        media_files=[
            MediaFile(id=1, item_id=1, file_path="clip.mp4", media_type="video", **media_kwargs)
        ],
    )


def test_on_screen_text_is_labelled_separately_from_the_visual_description() -> None:
    doc = build_content_document(
        _item(
            vision_caption="a person in a kitchen holding a bowl",
            ocr_text="CHILI LIME CHICKEN\n2 tbsp honey, 1 lime, 3 cloves garlic",
            transcript="",
        )
    )
    lines = doc.splitlines()
    assert "Media 1 visual description: a person in a kitchen holding a bowl" in lines
    assert any(line.startswith("Media 1 on-screen text: CHILI LIME CHICKEN") for line in lines)
    # OCR sits between the visual description and the transcript.
    assert doc.index("visual description") < doc.index("on-screen text")


def test_absent_ocr_text_adds_no_line() -> None:
    doc = build_content_document(_item(vision_caption="a cat", ocr_text=None, transcript="meow"))
    assert "on-screen text" not in doc
