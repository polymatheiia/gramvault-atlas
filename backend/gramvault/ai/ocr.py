"""On-screen text extraction for silent videos.

Instagram reels are frequently text-over-video: the caption overlay carries
the joke, the recipe, the claim — and for a reel with no narration and a
hashtag-only caption, that overlay is the only text about the post that
exists anywhere. Those items are otherwise unreachable by search.

This is deliberately *not* `pipeline._caption_video`. That asks the vision
model to describe a scene, which the local models do badly — measured on
this library, both `llava:7b` and `minicpm-v` called scored chicken breast
"fish" and then "salmon". Reading text off a frame is a different and much
more constrained task, and the same models do it well: minicpm-v
transcribed a small journal title off a laptop screen verbatim.

Reliability splits by script, so `clean_output` enforces what was measured
rather than trusting the model uniformly:

    English   excellent, including small body text
    Polish    good — occasional diacritic slips, semantically intact
    Cyrillic  unusable — either a false "NO TEXT", or Ukrainian text
              interleaved with Latin and even CJK characters

Cyrillic output is therefore discarded outright. A wrong transcription is
worse than an absent one once it is embedded, because retrieval cannot
tell them apart.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Asking for a bare transcription (no commentary, no translation) made the
# model answer "NO TEXT" on frames that plainly had Cyrillic text on them.
# The looser phrasing at least gets Latin-script text out intact; the
# commentary it adds is stripped below.
OCR_PROMPT = (
    "Transcribe all text visible in this image, exactly as written, preserving the "
    "original language and spelling. If there is no text, reply exactly: NO TEXT. "
    "Do not describe the image."
)

# minicpm-v wraps its answer in markdown scaffolding and appends asides.
_LABEL_RE = re.compile(
    r"^\s*\**\s*(title|body text|body|text|caption|image caption|image text structure|"
    r"header|subtitle|note|structure|main content|main tweet content|other elements|"
    r"engagement metrics|map features|additional information|additional details|"
    r"visible text|text elements?|overall)\b[^:\n]*:?\s*\**\s*",
    re.IGNORECASE | re.MULTILINE,
)
_PARENTHETICAL_NOTE_RE = re.compile(r"\((?:note|the text)\b[^)]*\)?", re.IGNORECASE | re.DOTALL)
_BRACKETED_DESCRIPTION_RE = re.compile(r"\[[^\]]*\]")
_MARKDOWN_RE = re.compile(r"[*_`#]+")
_LIST_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+", re.MULTILINE)

_NO_TEXT_MARKERS = {"no text", "no text.", "notext"}

# Despite "Do not describe the image", the model sometimes describes it
# anyway. Those replies must be dropped rather than stored: scene
# description is precisely the task these models are unreliable at (the
# reason this module exists separately from `pipeline._caption_video`), so
# a description that slips through is exactly the wrong text to embed.
_DESCRIPTION_OPENERS = (
    "there is", "there are", "the image", "this image", "this is a photo",
    "this is an image", "in the image", "the picture", "this picture",
    "the photo", "it appears", "a photo of", "an image of", "image of",
    "the scene", "the rest of", "the video", "this video", "video shows",
    "the following", "a screenshot of", "screenshot of",
)
_DESCRIPTION_PHRASES = (
    "in the image", "the image shows", "image shows", "appears to be",
    "the text is not", "no visible text", "there is no text",
    "rest of the video", "consists of various", "the video consists",
)

# The model also declines in prose instead of answering "NO TEXT" — often a
# fluent paragraph explaining that it cannot transcribe. That reads as
# ordinary text to every other check here, so it needs its own.
# Two signals, both required. "I'm sorry" alone is a perfectly ordinary
# thing for a meme overlay to say ("I'm sorry for what I said when I was
# hungry") — it only indicates a refusal when paired with the model talking
# about the task it won't do.
_INABILITY_RE = re.compile(
    r"\b(i'?m sorry|i am sorry|i cannot|i can'?t|i am unable|i'?m unable|"
    r"as an ai|i don'?t see|it seems that you)\b",
    re.IGNORECASE,
)
_TASK_NOUN_RE = re.compile(
    r"\b(transcrib\w*|image|images|picture|photo|provide[d]?|text)\b", re.IGNORECASE
)


def is_refusal(text: str) -> bool:
    """True if the reply is the model declining rather than transcribing.

    Requires the model to both express inability *and* refer to the task,
    so an overlay that merely sounds apologetic isn't thrown away.
    """
    head = text[:300]
    return bool(_INABILITY_RE.search(head) and _TASK_NOUN_RE.search(head))

# Shortest run of characters still worth embedding as on-screen text.
# Short overlays are the norm in this corpus, not noise — "a meowseum",
# "hair theory.", "11/10 Snake" are each the entire post, and a floor of 12
# (carried over from transcript filtering, where a sub-12-char result really
# is a Whisper artifact) discarded all three. The refusal and description
# filters do the real work of rejecting junk; this only needs to catch a
# stray word.
MIN_OCR_CHARS = 6


def has_cyrillic(text: str) -> bool:
    """True if any character is Cyrillic — the signal that this output came
    from the script the local models cannot read reliably."""
    return any("CYRILLIC" in unicodedata.name(ch, "") for ch in text if ch.isalpha())


def clean_output(raw: str | None) -> str | None:
    """Reduce a raw model reply to usable on-screen text, or None to discard.

    Discards: an explicit "NO TEXT", anything containing Cyrillic (see the
    module docstring), and anything too short to carry meaning once the
    model's markdown scaffolding is removed.
    """
    if not raw:
        return None

    text = _PARENTHETICAL_NOTE_RE.sub(" ", raw)
    text = _BRACKETED_DESCRIPTION_RE.sub(" ", text)
    text = _LABEL_RE.sub("", text)
    text = _LIST_BULLET_RE.sub("", text)
    text = _MARKDOWN_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()

    # Stripping a label can expose the description it introduced — the model
    # writes "Body Text: The rest of the video consists of..." and removing
    # the label alone leaves the description behind, embedded as if it were
    # text on screen. Drop those lines individually rather than judging the
    # reply as a whole, since a genuine transcription and a description
    # frequently arrive in the same reply.
    kept = [line for line in text.split("\n") if line.strip() and not is_description(line)]
    text = "\n".join(kept).strip()

    if not text or text.strip().lower() in _NO_TEXT_MARKERS:
        return None
    # A reply that merely contains the marker among other noise is still a
    # non-answer — e.g. "NO TEXT (the image shows a cat)".
    if text.strip().lower().startswith("no text"):
        return None
    if has_cyrillic(text):
        return None
    if is_refusal(text):
        return None
    if is_description(text):
        return None
    if len(text) < MIN_OCR_CHARS:
        return None
    return text


def is_description(text: str) -> bool:
    """True if the reply reads as a description of the picture rather than a
    transcription of text in it."""
    lowered = text.strip().lower()
    if lowered.startswith(_DESCRIPTION_OPENERS):
        return True
    return any(phrase in lowered for phrase in _DESCRIPTION_PHRASES)


def frame_for_ocr(video_path: Path, out_path: Path, at_seconds: float = 2.0) -> Path | None:
    """Grab a single frame to read text from.

    Two seconds in rather than frame zero: reels routinely open on a fade,
    a logo sting, or a blank frame, and the overlay is usually up by then.
    Scaled to 768px on the long edge — enough for small body text, while
    keeping the base64 payload to the model manageable.
    """
    import subprocess

    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-ss", str(at_seconds), "-i", str(video_path),
            "-vf", "scale=-2:768", "-frames:v", "1", "-qscale:v", "3", str(out_path),
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not out_path.exists():
        return None
    return out_path
