"""Work out what the user dropped on us.

Two questions, both answered without asking the user anything:
  1. Which file is the audio and which is the book? (by extension)
  2. What language is the book in? (lingua, over text pulled from the epub)

The detected language is a default, not a verdict - the UI always lets the user
override it, because getting this wrong wastes a long alignment run.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import languages
from .runner import AUDIO_SUFFIXES, TEXT_SUFFIXES

# Enough text to be confident without reading a whole book into memory.
_SAMPLE_CHARS = 20_000


class DetectionError(ValueError):
    pass


# Cover art for the rendered video. Optional, and gated behind sign-in.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


@dataclass(frozen=True)
class Pairing:
    # One entry for a single-file audiobook, many for a per-chapter set, in
    # playback order.
    audio_names: list[str]
    text_name: str
    # A picture to put behind the video, if one was dropped in.
    cover_name: str | None = None

    @property
    def is_multipart(self) -> bool:
        return len(self.audio_names) > 1

    @property
    def display_name(self) -> str:
        if not self.is_multipart:
            return self.audio_names[0]
        return f"{self.audio_names[0]} + {len(self.audio_names) - 1} more"


def natural_key(name: str) -> tuple:
    """Sort key that orders 2.mp3 before 10.mp3.

    Chapter order is playback order, and lexical sorting gets it wrong as soon
    as a set passes nine files.
    """
    parts = re.split(r"(\d+)", Path(name).stem)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def classify(filenames: list[str]) -> Pairing:
    """Split dropped filenames into the audio part(s), the text, and any cover."""
    audio = [n for n in filenames if Path(n).suffix.lower() in AUDIO_SUFFIXES]
    text = [n for n in filenames if Path(n).suffix.lower() in TEXT_SUFFIXES]
    images = [n for n in filenames if Path(n).suffix.lower() in IMAGE_SUFFIXES]

    if not audio:
        raise DetectionError(
            "No audio file found. Add an audiobook "
            "(m4b, mp3, m4a, opus, flac, wav...)."
        )
    if not text:
        raise DetectionError(
            "No book file found. Add an epub (or a txt/srt/vtt/ass script)."
        )
    if len(text) > 1:
        raise DetectionError(f"Got {len(text)} text files. Drop one book at a time.")

    # Mixed containers usually mean two different rips got dropped together.
    suffixes = {Path(n).suffix.lower() for n in audio}
    if len(suffixes) > 1:
        raise DetectionError(
            "The audio files are not all the same format ("
            + ", ".join(sorted(suffixes))
            + "). Drop one audiobook at a time."
        )

    return Pairing(
        audio_names=sorted(audio, key=natural_key),
        text_name=text[0],
        # Last one wins, so re-dropping an image replaces the previous choice.
        cover_name=images[-1] if images else None,
    )


def extract_text_sample(path: Path) -> str:
    """Pull readable text out of an epub (or plain text file) for detection."""
    suffix = path.suffix.lower()

    if suffix != ".epub":
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:_SAMPLE_CHARS]
        except OSError as exc:
            raise DetectionError(f"Could not read {path.name}: {exc}") from exc

    # Read the epub as a zip rather than via ebooklib: much faster, and it
    # tolerates the malformed epubs that converted books often are.
    try:
        from bs4 import BeautifulSoup

        chunks: list[str] = []
        total = 0
        with zipfile.ZipFile(path) as zf:
            names = [
                n for n in zf.namelist()
                if n.lower().endswith((".xhtml", ".html", ".htm"))
            ]
            for name in sorted(names):
                if total >= _SAMPLE_CHARS:
                    break
                try:
                    raw = zf.read(name).decode("utf-8", errors="replace")
                except (KeyError, OSError):
                    continue
                text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
                if text:
                    chunks.append(text)
                    total += len(text)
        sample = " ".join(chunks)[:_SAMPLE_CHARS]
    except zipfile.BadZipFile as exc:
        raise DetectionError(
            f"{path.name} is not a readable epub (bad zip archive)."
        ) from exc

    if not sample.strip():
        raise DetectionError(
            f"No text could be read from {path.name}. "
            "If it is a scanned/image-only book, it cannot be aligned."
        )
    return sample


@lru_cache(maxsize=1)
def _detector():
    # Built once and cached: constructing this is the expensive part.
    from lingua import LanguageDetectorBuilder

    return LanguageDetectorBuilder.from_all_languages().build()


@dataclass(frozen=True)
class Detection:
    code: str | None
    name: str | None
    confidence: float
    supported: bool


def detect_language(sample: str) -> Detection:
    """Best-guess ISO 639-1 code for a block of text."""
    if not sample.strip():
        return Detection(None, None, 0.0, False)

    values = _detector().compute_language_confidence_values(sample)
    if not values:
        return Detection(None, None, 0.0, False)

    best = values[0]
    code = best.language.iso_code_639_1.name.lower()
    known = languages.get(code)
    return Detection(
        code=code,
        name=known.name if known else best.language.name.title(),
        confidence=float(best.value),
        supported=known is not None,
    )
