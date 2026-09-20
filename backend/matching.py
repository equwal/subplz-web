"""Does this text actually match this audio?

subplz fails late and unhelpfully when it does not: it transcribes the whole
book first, then reports "the generated transcript and the provided text file
are too different" and writes a .subfail. On CPU that is a wasted hour.

So we ask the same question up front, cheaply: transcribe a couple of short
samples from the start of a few chapters, and score them against the book with
the backend's own rule. Same arithmetic, same threshold, ~10 seconds instead of
an hour.

What the number means, and what it does not: a good score says the text lines
up with what is being read. A bad score usually means the wrong book, the wrong
edition (abridged vs full), an audiobook that opens with a publisher
announcement the book does not contain, or a book buried under front matter.
"""

from __future__ import annotations

import json
import logging
import subprocess
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .settings import settings

log = logging.getLogger(__name__)

# Scoring compares chapter openings, so a sample from the start of a chapter is
# the relevant thing to transcribe - and a short one is enough. subplz caps the
# comparison at 2000 characters, which is a couple of minutes of speech.
SAMPLE_SECONDS = 60
MAX_SAMPLES = 3


@dataclass
class ChapterScore:
    audio_chapter: int
    best_score: float
    best_text_chapter: int | None
    transcript_head: str = ""

    @property
    def matched(self) -> bool:
        return self.best_text_chapter is not None


@dataclass
class MatchReport:
    threshold: float
    scores: list[ChapterScore] = field(default_factory=list)
    text_chapters: int = 0
    audio_chapters: int = 0
    verdict: str = "unknown"  # good | marginal | poor | unknown
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    skipped: str | None = None

    @property
    def best(self) -> float:
        return max((s.best_score for s in self.scores), default=0.0)

    @property
    def worst(self) -> float:
        return min((s.best_score for s in self.scores), default=0.0)

    @property
    def matched(self) -> int:
        return sum(1 for s in self.scores if s.matched)

    def as_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "verdict": self.verdict,
            "summary": self.summary,
            "best": round(self.best, 1),
            "worst": round(self.worst, 1),
            "sampled": len(self.scores),
            "matched": self.matched,
            "text_chapters": self.text_chapters,
            "audio_chapters": self.audio_chapters,
            "warnings": self.warnings,
            "skipped": self.skipped,
            "scores": [
                {
                    "audio_chapter": s.audio_chapter,
                    "score": round(s.best_score, 1),
                    "text_chapter": s.best_text_chapter,
                }
                for s in self.scores
            ],
        }


# ---------------------------------------------------------------------------
# book text
# ---------------------------------------------------------------------------

def book_chapters(text_path: Path) -> list[str]:
    """The comparable units, split the way the backend will split them.

    This matters more than it looks. subplz gives an epub one text chapter per
    spine document, but a .txt exactly one chapter for the whole file - so a
    flat text file offers the matcher a single anchor no matter how many
    chapters the audio has.
    """
    if text_path.suffix.lower() != ".epub":
        return [text_path.read_text(encoding="utf-8", errors="replace")]

    try:
        from bs4 import BeautifulSoup

        chapters: list[str] = []
        with zipfile.ZipFile(text_path) as zf:
            names = [
                n for n in zf.namelist()
                if n.lower().endswith((".xhtml", ".html", ".htm"))
            ]
            for name in sorted(names):
                try:
                    raw = zf.read(name).decode("utf-8", errors="replace")
                except (KeyError, OSError):
                    continue
                soup = BeautifulSoup(raw, "html.parser")
                for bad in soup(["script", "style"]):
                    bad.decompose()
                # subplz joins paragraph texts with no separator, so match that.
                text = "".join(
                    p.get_text(" ", strip=True) for p in soup.find_all("p")
                ) or soup.get_text(" ", strip=True)
                if text.strip():
                    chapters.append(text)
        return chapters
    except zipfile.BadZipFile:
        return []


# ---------------------------------------------------------------------------
# audio sampling
# ---------------------------------------------------------------------------

def audio_chapter_starts(audio: Path) -> list[float]:
    """Start time of each chapter, or [0.0] for a flat file."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-print_format", "json",
             str(audio)],
            capture_output=True, timeout=120, check=True,
        )
        chapters = json.loads(out.stdout.decode("utf-8")).get("chapters", [])
        starts = [float(c["start_time"]) for c in chapters]
        return starts or [0.0]
    except (subprocess.SubprocessError, ValueError, OSError, KeyError):
        return [0.0]


def read_samples(audio: Path, start: float, seconds: int):
    """`seconds` of audio from `start`, as the float32 mono 16 kHz the model wants."""
    import numpy as np

    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-max_error_rate", "1.0", "-err_detect", "ignore_err",
            "-ss", f"{start:.3f}", "-t", str(seconds), "-i", str(audio),
            "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", "16000", "-",
        ],
        capture_output=True, timeout=300,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, np.int16).astype(np.float32) / 32768.0


@lru_cache(maxsize=1)
def _model():
    """The same tiny model the alignment itself uses, loaded once."""
    from faster_whisper import WhisperModel

    return WhisperModel(settings.model, device="cpu", compute_type="int8")


def transcribe_sample(samples, language: str) -> str:
    segments, _ = _model().transcribe(
        samples, language=language, beam_size=1, without_timestamps=True
    )
    return "".join(seg.text for seg in segments)


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------

def check(audio: Path, text: Path, language: str, aligner) -> MatchReport:
    """Score a sample of the audio against the book. Never raises."""
    report = MatchReport(threshold=aligner.match_threshold)

    if not settings.match_check:
        report.skipped = "disabled"
        return report

    try:
        chapters = book_chapters(text)
        report.text_chapters = len(chapters)
        if not chapters:
            report.verdict = "poor"
            report.summary = "No readable text could be found in the book."
            return report

        starts = audio_chapter_starts(audio)
        report.audio_chapters = len(starts)

        if len(chapters) == 1 and len(starts) > 1:
            report.warnings.append(
                f"The book is one flat document but the audio has "
                f"{len(starts)} chapters. Alignment still works, but it has "
                f"only one place to anchor - an epub with real chapters aligns "
                f"more reliably."
            )

        # Sample from the start of chapters spread across the book, because the
        # score is about how chapters open.
        picks = _spread(len(starts), MAX_SAMPLES)
        scorer = (
            aligner.for_language(language)
            if hasattr(aligner, "for_language") else aligner
        )

        for idx in picks:
            samples = read_samples(audio, starts[idx], SAMPLE_SECONDS)
            if samples is None or len(samples) < 16000:
                continue
            transcript = transcribe_sample(samples, language)
            if len(transcript.strip()) < 40:
                continue

            best, best_i = 0.0, None
            for ci, chapter in enumerate(chapters):
                score = scorer.score_pair(transcript, chapter)
                if score > best:
                    best, best_i = score, ci

            report.scores.append(
                ChapterScore(
                    audio_chapter=idx,
                    best_score=best,
                    best_text_chapter=best_i if best > report.threshold else None,
                    transcript_head=transcript.strip()[:160],
                )
            )

        _verdict(report)
        return report

    except Exception as exc:  # noqa: BLE001 - a check must never block an upload
        log.warning("match check failed: %s", exc, exc_info=True)
        report.skipped = str(exc)
        return report


def _spread(n: int, k: int) -> list[int]:
    """Up to k indices spread across range(n), always including the first."""
    if n <= k:
        return list(range(n))
    step = n / k
    return sorted({min(n - 1, int(i * step)) for i in range(k)})


def _verdict(report: MatchReport) -> None:
    """Turn the raw score into advice.

    The bands sit well above the backend's own threshold, deliberately.
    Measured on real files: the right book scored 74, while a completely
    unrelated Russian novel still scored 40.5 - barely clearing subplz's
    threshold of 40. Two prose texts in the same language are roughly 40%
    similar character-by-character whatever they say, so "just over the
    threshold" means "probably wrong", not "probably fine".
    """
    if not report.scores:
        report.verdict = "unknown"
        report.summary = "Could not sample enough audio to check the match."
        return

    t = report.threshold or 40.0
    good_at, weak_at = t * 1.5, t * 1.2  # 60 and 48 for subplz
    best = report.best
    total = len(report.scores)
    strong = sum(1 for s in report.scores if s.best_score >= good_at)

    if best >= good_at:
        report.verdict = "good"
        report.summary = f"Text and audio line up ({best:.0f}/100 on the best sample)."
        if total > 1 and strong < total:
            report.warnings.append(
                f"Only {strong} of {total} sampled chapters scored well. The "
                f"timing may drift in parts of the book."
            )
    elif best >= weak_at:
        report.verdict = "marginal"
        report.summary = (
            f"Weak match ({best:.0f}/100). This may be a different edition or "
            f"an abridgement. Alignment can still work, but expect drift."
        )
    else:
        report.verdict = "poor"
        report.summary = (
            f"This text does not look like this audio ({best:.0f}/100). Two "
            f"unrelated books in the same language score about this well, so "
            f"it is probably the wrong book, edition or abridgement."
        )
