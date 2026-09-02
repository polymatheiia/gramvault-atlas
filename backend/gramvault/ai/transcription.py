"""faster-whisper wrapper for audio/video transcription.

Used by `gramvault.ai.pipeline` to transcribe the audio track of
videos/reels for embedding + citation display. Runs fully locally (no
network calls) via the `faster-whisper` CTranslate2-based Whisper
implementation.

The whisper model itself is expensive to load, so it's cached per
(model size, device, compute type) in-process. The actual `faster_whisper`
import happens lazily inside `_load_model()` so that:
  - the module can be imported (and its pure functions unit-tested) even in
    environments where `faster-whisper` isn't installed yet, and
  - tests can monkeypatch `_load_model` directly instead of needing a real
    model file on disk.

Model size/device/compute type aren't in `config.yaml` (that file is owned
by Agent A1's scaffold and lists only the Ollama model names) — they're
read from environment variables with sensible CPU-friendly defaults so
they're still configurable without hardcoding, without needing to touch
`gramvault/config.py`:
    GRAMVAULT_WHISPER_MODEL_SIZE   (default: "base")
    GRAMVAULT_WHISPER_DEVICE       (default: "cpu")
    GRAMVAULT_WHISPER_COMPUTE_TYPE (default: "int8")
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

# "turbo" (large-v3-turbo) rather than a smaller model: on non-English
# audio the small models don't degrade gracefully, they produce confident
# non-words — measured here, Ukrainian "толерантність ... ГБТ спільноти"
# came back from `small` as "певерантність ... вибетестлю ноти". Wrong text
# is worse than no text once it's embedded, and it's indistinguishable from
# real text downstream. Turbo costs roughly 2x the wall clock on CPU; set
# GRAMVAULT_WHISPER_MODEL_SIZE=small to trade that back if the library is
# predominantly English.
DEFAULT_MODEL_SIZE = "turbo"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"

# Letters unique (or near-unique) to a language among the Latin-script
# languages that actually show up in saved Instagram content. Only used to
# nudge Whisper's language selection, so a miss costs nothing beyond
# falling back to auto-detection.
_LATIN_LANGUAGE_MARKERS = {
    "pl": set("ąćęłńśźż"),
    "tr": set("ğışİ"),
    "pt": set("ãõç"),
    "de": set("äöüß"),
}

# Function words that are common in English captions and rare as bare words
# in the other languages here — two hits is enough to call it English.
_ENGLISH_STOPWORDS = frozenset(
    {
        "the", "and", "you", "your", "for", "this", "that", "with", "have",
        "from", "what", "when", "how", "are", "was", "will", "can", "just",
        "about", "they", "them", "all", "not", "but", "out", "get", "make",
        "more", "here", "some", "like", "who", "why", "these", "would",
    }
)

# Unicode script name fragment -> ISO-639-1 code.
_SCRIPT_LANGUAGES = [
    ("HIRAGANA", "ja"),
    ("KATAKANA", "ja"),
    ("HANGUL", "ko"),
    ("CYRILLIC", "uk"),  # Ukrainian is the dominant Cyrillic source here
    ("GREEK", "el"),
    ("ARABIC", "ar"),
    ("HEBREW", "he"),
    ("THAI", "th"),
    ("DEVANAGARI", "hi"),
]


def language_hint(text: str | None) -> str | None:
    """Best-effort ISO-639-1 hint for a post's spoken language, derived
    from its written caption.

    Whisper auto-detects from a short leading window of audio, which on a
    reel is usually the music intro — in testing it read a Ukrainian reel
    as English and a Polish one as Norwegian. The caption is a better
    signal for the same question, and it's already in the database.

    Returns None when there's no usable signal, which leaves Whisper's
    own detection in place.
    """
    if not text or not text.strip():
        return None

    # Non-Latin script anywhere in the caption is decisive — a Cyrillic or
    # CJK run doesn't appear by accident in an otherwise English caption.
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        for fragment, code in _SCRIPT_LANGUAGES:
            if fragment in name:
                return code

    lowered = text.lower()
    for code, markers in _LATIN_LANGUAGE_MARKERS.items():
        if markers & set(lowered):
            return code

    # English needs positive evidence, not just an absence of diacritics.
    # Plenty of German, Dutch, or Indonesian captions are pure ASCII, and
    # forcing "en" onto them produces exactly the confident garbage this
    # hint exists to prevent — better to return None and let Whisper decide.
    words = set(re.findall(r"[a-z']+", text.lower()))
    if len(words & _ENGLISH_STOPWORDS) >= 2:
        return "en"
    return None


def _whisper_model_size() -> str:
    return os.environ.get("GRAMVAULT_WHISPER_MODEL_SIZE", DEFAULT_MODEL_SIZE)


def _whisper_device() -> str:
    return os.environ.get("GRAMVAULT_WHISPER_DEVICE", DEFAULT_DEVICE)


def _whisper_compute_type() -> str:
    return os.environ.get("GRAMVAULT_WHISPER_COMPUTE_TYPE", DEFAULT_COMPUTE_TYPE)


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str | None = None


class WhisperNotAvailableError(RuntimeError):
    """Raised when the `faster-whisper` package isn't installed."""

    def __init__(self, cause: Exception | None = None) -> None:
        super().__init__(
            "The 'faster-whisper' package is not installed, but it's required "
            "to transcribe audio/video.\n"
            "Install it with: pip install faster-whisper\n"
            "(it should already be listed in pyproject.toml — try "
            "`pip install -e \".[dev]\"` from the repo root)."
        )
        self.cause = cause


# Whisper emits these when handed silence, music, or noise — they're
# artifacts of the subtitled video its training data was drawn from, not
# anything present in the audio. Left in place they embed as real text and
# make every silent reel a near-match for every other one.
_HALLUCINATED_TRANSCRIPTS = frozenset(
    {
        "you",
        "you.",
        "oh",
        "oh.",
        "okay.",
        "bye.",
        "thank you.",
        "thank you very much.",
        "thanks for watching!",
        "thanks for watching.",
        "thank you for watching!",
        "subscribe!",
        "music",
        "[music]",
        "...",
        # Subtitle-credit boilerplate, in the languages seen in this corpus.
        "napisy stworzone przez społeczność amara.org",
        "napisy: aleksandra malinowska",
        "danke fürs zuschauen!",
        "редактор субтитров о.голубки",
        "субтитры сделал диматоржок",
        "продолжение следует...",
    }
)

# Below this, a transcript is a fragment rather than content. Chosen from
# the observed distribution: cutting here drops ~35% of non-empty results
# while keeping ~98% of the total transcribed text.
MIN_USEFUL_TRANSCRIPT_CHARS = 60


def is_noise(text: str) -> bool:
    """True if `text` is a Whisper artifact rather than real speech.

    Two signals: an exact match against known hallucinated phrases, or a
    transcript too short to carry retrievable meaning. Deliberately
    conservative — it only inspects the text, so it can be applied after
    the fact to transcripts already in the database without re-running
    the model.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    # Phrase check first: some boilerplate is longer than the length floor,
    # so a length short-circuit would let it through.
    if stripped.lower() in _HALLUCINATED_TRANSCRIPTS:
        return True
    return len(stripped) < MIN_USEFUL_TRANSCRIPT_CHARS


@lru_cache(maxsize=1)
def _load_model(model_size: str, device: str, compute_type: str) -> Any:
    """Load (and cache) a faster-whisper `WhisperModel`. Separated into its
    own function so tests can monkeypatch it without a real model file."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise WhisperNotAvailableError(exc) from exc
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def transcribe(media_path: Path, language: str | None = None) -> TranscriptionResult:
    """Transcribe the audio track of `media_path` (video or audio file).

    Returns an empty-text `TranscriptionResult` (not an error) if the file
    has no audio track or no speech is detected — that's an expected,
    common case (e.g. a muted video/reel), not a failure.

    `language` is an optional ISO-639-1 hint (e.g. "uk", "pl"). Whisper's
    auto-detection runs on a short leading window, which on a reel is
    usually the music intro rather than the narration — it misidentifies
    the language often enough to be worth overriding whenever the caller
    has a better guess (see `cli.transcribe`, which derives one from the
    post's caption).
    """
    media_path = Path(media_path)
    model = _load_model(_whisper_model_size(), _whisper_device(), _whisper_compute_type())

    try:
        segments_iter, info = model.transcribe(
            str(media_path),
            # Reels are overwhelmingly music-backed. Without voice-activity
            # detection Whisper transcribes the soundtrack, inventing lyrics
            # for instrumentals — confident, fluent, and entirely unrelated
            # to the video. VAD makes a music-only reel return nothing,
            # which is the correct answer.
            vad_filter=True,
            language=language,
            # Each reel is independent, and carrying context across a short
            # clip encourages the model to extend a hallucinated phrase
            # rather than reset.
            condition_on_previous_text=False,
        )
    except Exception:
        # faster-whisper/ffmpeg (used internally for audio decoding) can
        # raise a variety of exceptions for "no audio track"/corrupt media.
        # Treat any of them as "nothing to transcribe" rather than a hard
        # pipeline failure, per the task's "handle gracefully" requirement.
        return TranscriptionResult(text="")

    segments: list[TranscriptSegment] = [
        TranscriptSegment(start=float(seg.start), end=float(seg.end), text=seg.text.strip())
        for seg in segments_iter
    ]
    text = " ".join(seg.text for seg in segments if seg.text)
    language = getattr(info, "language", None)
    if is_noise(text):
        # Report it the same way as a genuinely silent file: no text, but
        # keep the segments and detected language for anyone debugging why
        # a given reel produced nothing.
        return TranscriptionResult(text="", segments=segments, language=language)
    return TranscriptionResult(text=text, segments=segments, language=language)
