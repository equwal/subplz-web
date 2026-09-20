"""The alignment backend, behind an interface.

Everything subplz-specific lives in `SubPlzAligner`: its argument names, its
Japanese defaults, the shape of its progress output, where it writes the result.
The rest of the application talks to the `Aligner` interface, so replacing
subplz means writing one new class and changing SUBPLZ_WEB_ALIGNER - not
touching the API, the queue, storage or the job runner.

To add a backend:
  1. subclass Aligner
  2. register it in ALIGNERS below
  3. set SUBPLZ_WEB_ALIGNER to its name
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from . import languages
from .settings import settings


@dataclass(frozen=True)
class AlignRequest:
    """Everything a backend needs to align one book."""

    audio: Path
    text: Path
    out_dir: Path
    language: str
    model: str
    device: str
    threads: int
    # Chapter count of the audio, or 1. Backends may use it for progress.
    chapters: int = 1


@dataclass(frozen=True)
class ProgressUpdate:
    stage: str
    # 0..1 within the alignment run, or None to leave the bar where it is.
    fraction: float | None = None


class ProgressReader(ABC):
    """Per-run state for interpreting a backend's console output."""

    @abstractmethod
    def feed(self, line: str) -> ProgressUpdate | None:
        """Interpret one output line. Return None if it says nothing useful."""


class Aligner(ABC):
    name: str = "aligner"
    output_suffix: str = ".srt"

    # Score at or below which this backend refuses to pair audio with text.
    # 0 means the backend has no such notion.
    match_threshold: float = 0.0

    def score_pair(self, audio_text: str, book_text: str) -> float:
        """How well a transcript of some audio matches a piece of the book.

        Same arithmetic the backend uses internally, so a preflight number
        means the same thing as the one that decides a real run.
        """
        return 0.0

    @abstractmethod
    def build_command(self, req: AlignRequest) -> list[str]:
        """The subprocess to run."""

    @abstractmethod
    def progress_reader(self, req: AlignRequest) -> ProgressReader:
        ...

    @abstractmethod
    def locate_output(self, req: AlignRequest) -> Path | None:
        """The subtitle file produced, or None if there is not one."""

    def environment(self) -> dict[str, str]:
        """Environment for the subprocess."""
        env = os.environ.copy()
        # Backends print emoji; without this a piped stdout dies on a
        # cp1252/cp932 console.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def language_note(self, code: str) -> str | None:
        """Anything the user should know about this language, or None."""
        return None


# ---------------------------------------------------------------------------
# subplz
# ---------------------------------------------------------------------------

# subplz transcribes one chapter at a time and each chapter gets its own bar
# running 0->100%. Matching only the "Transcribe:" bar keeps the sentence
# splitting and grouping bars from yanking the number around.
_TRANSCRIBE_PCT = re.compile(r"Transcribe:\s*(\d{1,3})%")

_STAGES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Starting '"), "Loading audio"),
    (re.compile(r"Fuzzy matching chapters"), "Matching chapters"),
    (re.compile(r"Splitting transcript into sentences"), "Splitting text into sentences"),
    (re.compile(r"Syncing"), "Aligning audio to text"),
    (re.compile(r"Grouping based on transcript"), "Grouping subtitle lines"),
    (re.compile(r"Writing generated subs"), "Writing subtitles"),
]

# Transcription dominates the wall clock; the later phases share the tail.
_RUN_START = 0.05
_TRANSCRIBE_END = 0.80

_STAGE_PROGRESS: dict[str, float] = {
    "Aligning audio to text": 0.82,
    "Grouping subtitle lines": 0.86,
    "Writing subtitles": 0.89,
}


def _subplz_clean(text: str, lang_code: str) -> str:
    """What subplz feeds to fuzz.ratio: lang.normalize(lang.clean(text)).

    Falls back to the library's own implementation when it is importable, so
    this cannot drift from the real thing; the inline version is only a
    stand-in for when ats is not installed.
    """
    try:
        from ats.lang import get_lang

        lang = get_lang(lang_code)
        return lang.normalize(lang.clean(text))
    except Exception:  # noqa: BLE001 - scoring must never break an upload
        import unicodedata

        return unicodedata.normalize("NFKD", text.lower())


class _SubPlzProgress(ProgressReader):
    def __init__(self, chapters: int):
        self.chapters = max(1, chapters)
        self.done = 0
        self.last_pct = 0
        self.stage = "Starting subplz"
        self.best = _RUN_START

    def feed(self, line: str) -> ProgressUpdate | None:
        for pattern, label in _STAGES:
            if pattern.search(line):
                self.stage = label
                break

        fraction = None
        m = _TRANSCRIBE_PCT.search(line)
        if m:
            pct = min(100, max(0, int(m.group(1))))
            # The bar restarting means the previous chapter finished.
            if pct < self.last_pct:
                self.done = min(self.done + 1, self.chapters - 1)
            self.last_pct = pct
            frac = min(1.0, (self.done + pct / 100.0) / self.chapters)
            fraction = _RUN_START + frac * (_TRANSCRIBE_END - _RUN_START)
            self.stage = (
                f"Transcribing chapter {self.done + 1} of {self.chapters}"
                if self.chapters > 1
                else "Transcribing audio"
            )
        elif self.stage in _STAGE_PROGRESS:
            fraction = _STAGE_PROGRESS[self.stage]

        if fraction is not None:
            # Progress only ever moves forward.
            self.best = max(self.best, fraction)
        return ProgressUpdate(stage=self.stage, fraction=self.best)


class SubPlzAligner(Aligner):
    """kanjieater/SubPlz, driven through its `sync` subcommand.

    `sync` only ever times the text you supply. `gen`, which transcribes a book
    from scratch, is deliberately never invoked.
    """

    name = "subplz"
    output_suffix = ".srt"

    # subplz/sync.py: SCORE_THRESHOLD = 40. A chapter whose best fuzz.ratio
    # never exceeds this is reported as "too different" and left unmatched.
    match_threshold = 40.0

    def score_pair(self, audio_text: str, book_text: str) -> float:
        """Reproduces subplz's own match_start() scoring, exactly.

        Two details that are easy to get wrong and both matter a lot:

        * It compares only the **first min(len_a, len_b, 2000) characters**, so
          the score is about how chapters *open*, not how similar they are
          overall. Front matter or an unread heading at the top of a chapter
          sinks it even when the rest is identical.
        * Cleaning is language-dependent, and ats/lang.py only implements
          Japanese and English - every other language falls back to English,
          whose clean() is nothing but .lower(). So for Russian, Spanish,
          Portuguese and the rest, punctuation and spacing are compared
          verbatim.
        """
        from rapidfuzz import fuzz

        a = _subplz_clean(audio_text, self._lang_code)
        b = _subplz_clean(book_text, self._lang_code)
        # Below this, subplz does not even consider the pair.
        if len(a) < 100 or len(b) < 100:
            return 0.0
        n = min(len(a), len(b), 2000)
        return float(fuzz.ratio(a[:n], b[:n]))

    # Set per request so scoring matches the language subplz will run under.
    _lang_code: str = "en"

    def for_language(self, code: str) -> "SubPlzAligner":
        clone = SubPlzAligner()
        clone._lang_code = code
        return clone

    def build_command(self, req: AlignRequest) -> list[str]:
        lang = languages.require(req.language)

        cmd = [
            str(settings.subplz_bin), "sync",
            # subplz takes either -d, or all three of --audio/--text/
            # --output-dir, and rejects a mix. The explicit form pairs the files
            # by name rather than by directory sort order.
            "--audio", str(req.audio),
            "--text", str(req.text),
            "--output-dir", str(req.out_dir),
            "--output-format", "srt",
            # subplz defaults BOTH of these to Japanese - always set them.
            "--language", lang.code,
            "--lang", lang.code,
            "--lang-ext", lang.code,
            "--model", req.model,
            "--device", req.device,
            "--overwrite",
            "--rerun",
            "--progress",
            "--threads", str(req.threads),
        ]

        # pysbd cannot segment this language; make subplz use stanza instead.
        if lang.needs_nlp_flag:
            cmd.append("--nlp")

        return cmd

    def progress_reader(self, req: AlignRequest) -> ProgressReader:
        return _SubPlzProgress(req.chapters)

    def locate_output(self, req: AlignRequest) -> Path | None:
        # subplz writes <stem>.<lang-ext>.srt, and the stem is ours.
        expected = req.out_dir / f"{req.audio.stem}.{req.language}.srt"
        if expected.exists():
            return expected
        # Fall back in case upstream changes the convention.
        return next(iter(sorted(req.out_dir.glob("*.srt"))), None)

    def language_note(self, code: str) -> str | None:
        lang = languages.get(code)
        if lang is None or not lang.needs_nlp_flag:
            return None
        return (
            f"{lang.name} uses the stanza sentence splitter. "
            f"The first {lang.name} run downloads a small model."
        )


ALIGNERS: dict[str, type[Aligner]] = {
    SubPlzAligner.name: SubPlzAligner,
}


def get_aligner() -> Aligner:
    try:
        return ALIGNERS[settings.aligner]()
    except KeyError:
        known = ", ".join(sorted(ALIGNERS))
        raise RuntimeError(
            f"Unknown aligner {settings.aligner!r}. Known backends: {known}"
        ) from None


aligner: Aligner = get_aligner()
