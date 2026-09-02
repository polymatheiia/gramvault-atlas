"""Tests for gramvault.ai.transcription's text-level helpers: deriving a
language hint from a post's caption, and recognizing the artifacts Whisper
produces when handed silence or music.

Both operate on strings only — no model, no audio — so they're exercised
directly rather than through a mocked `WhisperModel`.
"""

from __future__ import annotations

import pytest

from gramvault.ai.transcription import (
    MIN_USEFUL_TRANSCRIPT_CHARS,
    is_noise,
    language_hint,
)


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("Наш улюблений рецепт приготування аеропресу", "uk"),
        ("KAŻDY produkt w TV, życzę wszystkiego wspaniałego", "pl"),
        ("Will you try this? Follow for more recipes", "en"),
        ("今夜、ハリウッドで開催されたイベントに登場しました", "ja"),
        ("Schöne Grüße, was für ein Stück!", "de"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_language_hint_detects_expected_language(caption: str | None, expected: str | None) -> None:
    assert language_hint(caption) == expected


def test_language_hint_declines_ascii_non_english() -> None:
    """A German caption that happens to use no umlauts is pure ASCII, and
    guessing "en" from that would force English decoding onto German audio
    — the exact failure this hint exists to prevent. None means "let
    Whisper decide", which is the safe answer.
    """
    assert language_hint("Was passiert, wenn man Klavier und Gesang zusammenbringt") is None


def test_language_hint_requires_real_english_evidence() -> None:
    """A couple of English function words is the positive signal; a bare
    ASCII product name is not enough."""
    assert language_hint("Follow me for more of this and you will love it") == "en"
    assert language_hint("Rossmann isana ziaja") is None


def test_language_hint_prefers_script_over_latin_markers() -> None:
    """A caption mixing Cyrillic with Polish diacritics is Cyrillic — script
    is the stronger signal, so it has to be checked first."""
    assert language_hint("Привіт! Zapraszam ąćę") == "uk"


def test_language_hint_ignores_captions_with_no_letters() -> None:
    """Emoji and punctuation carry no language signal, so the caller should
    fall back to Whisper's own detection rather than a bogus 'en'."""
    assert language_hint("🔥🔥🔥 ✨ #️⃣") is None


def test_is_noise_flags_empty_and_whitespace() -> None:
    assert is_noise("")
    assert is_noise("   \n ")


@pytest.mark.parametrize(
    "phrase",
    [
        "you",
        "You",
        "  Thanks for watching!  ",
        "napisy stworzone przez społeczność amara.org",
        "редактор субтитров о.голубки",
        "danke fürs zuschauen!",
    ],
)
def test_is_noise_flags_known_hallucinations(phrase: str) -> None:
    """Whisper emits subtitle credits and sign-offs over silence and music.
    Embedded, they make every silent reel a near-match for every other."""
    assert is_noise(phrase)


def test_is_noise_flags_boilerplate_longer_than_the_length_floor() -> None:
    """Some credit strings are longer than the length floor, so the phrase
    check must run before the length short-circuit rather than after it."""
    phrase = "napisy stworzone przez społeczność amara.org"
    assert len(phrase) >= MIN_USEFUL_TRANSCRIPT_CHARS - 20  # ordering matters here
    assert is_noise(phrase)


def test_is_noise_keeps_real_speech() -> None:
    real = (
        "Wyjaśniam wszystkie tryby w piekarniku, ponieważ 90 procent ludzi "
        "korzysta tylko z jednego."
    )
    assert len(real) >= MIN_USEFUL_TRANSCRIPT_CHARS
    assert not is_noise(real)


def test_is_noise_flags_short_fragments() -> None:
    assert is_noise("I got")
    assert is_noise("Oh my god! No job?")


def test_is_noise_boundary_is_inclusive_at_the_floor() -> None:
    """The cutoff is 'shorter than', so a transcript of exactly
    MIN_USEFUL_TRANSCRIPT_CHARS characters is kept."""
    assert not is_noise("x" * MIN_USEFUL_TRANSCRIPT_CHARS)
    assert is_noise("x" * (MIN_USEFUL_TRANSCRIPT_CHARS - 1))
