"""Tests for gramvault.ai.ocr's output cleaning.

The model's raw replies are wrapped in markdown scaffolding and asides, and
its Cyrillic transcription is unreliable enough to discard outright. These
are pure string transforms, so they're tested directly — the real model
calls are exercised by the CLI, not here.

Fixtures below are verbatim replies from `minicpm-v` observed while
evaluating this library, not invented examples.
"""

from __future__ import annotations

import pytest

from gramvault.ai.ocr import MIN_OCR_CHARS, clean_output, has_cyrillic


def test_clean_output_keeps_plain_transcription() -> None:
    raw = (
        "Skipping breakfast and its wide-ranging health consequences: A systematic "
        "review from multiple metabolic disruptions to socioeconomic factors"
    )
    assert clean_output(raw) == raw


def test_clean_output_strips_markdown_labels() -> None:
    """minicpm-v labels its answer rather than just answering."""
    assert clean_output("**Title:** hair theory.") == "hair theory."


def test_clean_output_strips_trailing_note() -> None:
    raw = '**Title:** hair theory.\n\n(Note: The title "hair theory." appears at the top.)'
    assert clean_output(raw) == "hair theory."


def test_clean_output_strips_bracketed_scene_descriptions() -> None:
    """It interleaves descriptions of non-text regions; those aren't text."""
    raw = "Kierowcy dziela sie na dwie typy:\n[Logos of various car brands]\n- KIA\n- VOLVO"
    cleaned = clean_output(raw)
    assert cleaned is not None
    assert "Logos of various" not in cleaned
    assert "KIA" in cleaned and "VOLVO" in cleaned


@pytest.mark.parametrize("raw", ["NO TEXT", "no text", "  No Text.  ", "NO TEXT (just a cat)"])
def test_clean_output_discards_no_text(raw: str) -> None:
    assert clean_output(raw) is None


def test_clean_output_discards_cyrillic() -> None:
    """Measured on this library, Cyrillic OCR comes back garbled — sometimes
    with Latin or CJK characters spliced in. Wrong text is worse than none
    once embedded, so it's dropped rather than stored."""
    garbled = 'Колли пред偏好 сксазав, шо постsidты залик, яко звижемите вuchtsid на konehlctyi'
    assert clean_output(garbled) is None


def test_clean_output_discards_partially_cyrillic() -> None:
    """Mixed-script output is the common failure shape, not pure Cyrillic."""
    assert clean_output('Title: Як виглядалèsе слово "Подивyмось"') is None


def test_clean_output_discards_empty_and_none() -> None:
    assert clean_output(None) is None
    assert clean_output("") is None
    assert clean_output("   \n  ") is None


def test_clean_output_discards_too_short() -> None:
    assert clean_output("ok") is None
    assert clean_output("x" * (MIN_OCR_CHARS - 1)) is None
    assert clean_output("x" * MIN_OCR_CHARS) == "x" * MIN_OCR_CHARS


def test_clean_output_keeps_polish_diacritics() -> None:
    """Latin-script diacritics are fine — only Cyrillic is unreliable."""
    raw = "Kierowcy dzielą się na dwa typy:"
    assert clean_output(raw) == raw


def test_has_cyrillic_distinguishes_scripts() -> None:
    assert has_cyrillic("Коли препод сказав")
    assert has_cyrillic("mixed Латиниця and latin")
    assert not has_cyrillic("Kierowcy dzielą się na dwa typy")
    assert not has_cyrillic("hair theory.")
    assert not has_cyrillic("123 !@# ---")


def test_clean_output_discards_scene_descriptions() -> None:
    """The model sometimes describes the picture despite being told not to.
    Scene description is the task these models are unreliable at, so a
    description that slips through is the worst thing to store."""
    raw = "There is a cat hanging out of an air vent in the image. The text is not legible."
    assert clean_output(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "The image shows two people cooking in a kitchen with fresh vegetables.",
        "This is a photograph capturing the process of cooking fish in a skillet.",
        "In the image, a woman holds a pink mug and smiles at the camera.",
        "It appears to be a slideshow of car logos on a dark background.",
    ],
)
def test_clean_output_discards_description_openers(raw: str) -> None:
    assert clean_output(raw) is None


def test_clean_output_keeps_transcription_that_merely_mentions_an_image() -> None:
    """Rejection keys on description *shape*, not any mention of pictures —
    an overlay can legitimately talk about images."""
    assert clean_output("POV: your camera roll is 90% screenshots") is not None
    assert clean_output("How to take better photos with just your phone") is not None


@pytest.mark.parametrize(
    "raw",
    [
        "I'm sorry, but the image you provided does not contain any text that can be transcribed.",
        "I'm sorry, but I am unable to transcribe or provide any information about images.",
        "I'm sorry, but it seems that you didn't provide an image to transcribe.",
        "As an AI, I cannot read the text in this picture.",
    ],
)
def test_clean_output_discards_refusals(raw: str) -> None:
    """The model declines in fluent prose rather than saying NO TEXT, which
    reads as ordinary text to every other check. Observed at 9% of stored
    rows before this was added."""
    assert clean_output(raw) is None


def test_clean_output_keeps_overlay_that_sounds_apologetic() -> None:
    """Rejection must key on the model refusing, not on a post whose own
    overlay happens to be an apology."""
    assert clean_output("I'm sorry for what I said when I was hungry") is not None
    assert clean_output("POV: you can't stop thinking about them") is not None


def test_clean_output_drops_description_lines_but_keeps_the_transcription() -> None:
    """The model labels its answer and then describes the picture under the
    same reply. Stripping the label alone leaves the description behind,
    which then embeds as if it were text on screen — so lines are judged
    individually rather than the reply as a whole."""
    raw = (
        'Title: "Get a bird feeder and you will see many cute birds"\n'
        "Body Text: The rest of the video consists of various birds"
    )
    assert clean_output(raw) == '"Get a bird feeder and you will see many cute birds"'


def test_clean_output_strips_scaffolding_labels_observed_in_the_wild() -> None:
    raw = "Structure:\nTitle (in bold): 11/10 Snake"
    assert clean_output(raw) == "11/10 Snake"


def test_clean_output_keeps_multiple_overlay_lines() -> None:
    raw = (
        "Title: Motivational speaker : Be fearless , you have a tiger in you\n"
        "Subtitle: The tiger in me :\n"
        "Image of a tiger in a cage"
    )
    cleaned = clean_output(raw)
    assert cleaned is not None
    assert "Be fearless" in cleaned and "The tiger in me" in cleaned
    assert "Image of a tiger" not in cleaned  # that line is a description


@pytest.mark.parametrize("overlay", ["a meowseum", "hair theory.", "I make it", "Kaufland"])
def test_clean_output_keeps_short_overlays(overlay: str) -> None:
    """Short overlays are frequently the whole post. The previous 12-char
    floor — inherited from transcript filtering — discarded all of these."""
    assert clean_output(overlay) == overlay
