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

from . import chapters as chapters_mod
from .settings import settings

log = logging.getLogger(__name__)

# Enough speech to fingerprint a chapter confidently. Sampling is adaptive, so
# a book that matches well normally pays for one of these and stops.
SAMPLE_SECONDS = 120
MAX_SAMPLES = 3


@dataclass
class ChapterScore:
    audio_chapter: int
    best_score: float
    best_text_chapter: int | None
    runner_up: float = 0.0
    # How far clear of the runner-up. 1.0 means every chapter looked alike,
    # which is the signature of the wrong book rather than a poor recording.
    confidence: float = 0.0
    accepted: bool = False
    transcript_head: str = ""

    @property
    def matched(self) -> bool:
        return self.accepted


@dataclass
class MatchReport:
    threshold: float
    scores: list[ChapterScore] = field(default_factory=list)
    text_chapters: int = 0
    audio_chapters: int = 0
    accept_at: float = 0.0
    noise_floor: float = 0.0
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

    @property
    def confidence(self) -> float:
        return max((s.confidence for s in self.scores), default=0.0)

    def as_dict(self) -> dict:
        return {
            "threshold": round(self.accept_at, 1),
            "noise_floor": round(self.noise_floor, 1),
            "confidence": round(self.confidence, 2),
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
                    "runner_up": round(s.runner_up, 1),
                    "confidence": round(s.confidence, 2),
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
        samples, language=language, beam_size=5, without_timestamps=True
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

        # Learn what this book scores by chance before judging any match
        # against it. See backend/chapters.py for why a fixed threshold cannot
        # work across scripts.
        fingerprints = [chapters_mod.fingerprint(c) for c in chapters]
        calibration = chapters_mod.calibrate(chapters)
        report.accept_at = calibration.accept_at
        report.noise_floor = calibration.floor

        # Sample chapters spread through the book, skipping the first: it is
        # where publisher announcements and credits live, so it is the least
        # representative chapter there is.
        picks = _spread(len(starts), MAX_SAMPLES)

        for idx in picks:
            samples = read_samples(audio, starts[idx], SAMPLE_SECONDS)
            if samples is None or len(samples) < 16000:
                continue
            transcript = transcribe_sample(samples, language)
            if len(transcript.strip()) < 40:
                continue

            match = chapters_mod.best_match(
                chapters_mod.fingerprint(transcript), fingerprints, calibration
            )
            report.scores.append(
                ChapterScore(
                    audio_chapter=idx,
                    best_score=match.score,
                    best_text_chapter=match.text_index,
                    runner_up=match.runner_up,
                    confidence=match.confidence,
                    accepted=match.accepted,
                    transcript_head=transcript.strip()[:160],
                )
            )
            # A confident match is enough; only keep sampling when unsure.
            if match.accepted and match.confidence >= 2.0:
                break

        _verdict(report)
        return report

    except Exception as exc:  # noqa: BLE001 - a check must never block an upload
        log.warning("match check failed: %s", exc, exc_info=True)
        report.skipped = str(exc)
        return report


def _spread(n: int, k: int) -> list[int]:
    """Up to k chapter indices spread across the book.

    Skips chapter 0 when there is anything else to choose. It is where
    publisher announcements, credits and "read by" cards live - material that
    is in the audio and not in the book - so it is the least representative
    chapter there is, and the worst one to judge a whole book on.
    """
    if n <= 1:
        return [0]
    first = 1 if n > 3 else 0
    usable = n - first
    if usable <= k:
        return list(range(first, n))
    step = usable / k
    return sorted({min(n - 1, first + int(i * step)) for i in range(k)})


def _verdict(report: MatchReport) -> None:
    """Turn the measurements into advice.

    Two separate questions, and the second is the one a fixed threshold cannot
    answer:

    * Is the best chapter similar enough to be a real match at all?
    * Is it *distinctly* the best, or did every chapter score alike?

    A different book in the same language scores high on the first and fails
    the second - its chapters all look equally plausible because they share a
    language, not a story.
    """
    if not report.scores:
        report.verdict = "unknown"
        report.summary = "Could not sample enough audio to check the match."
        return

    best = report.best
    confidence = report.confidence
    accepted = report.matched

    if accepted and confidence >= 2.0:
        report.verdict = "good"
        report.summary = (
            f"Text and audio line up - the matching chapter scores {best:.0f}, "
            f"{confidence:.1f}x clear of the next best."
        )
    elif accepted:
        report.verdict = "marginal"
        report.summary = (
            f"Probable match ({best:.0f}), but only {confidence:.1f}x clear of "
            f"the next best chapter. Expect some drift."
        )
    elif best >= report.accept_at:
        # Similar enough, but nothing stood out.
        report.verdict = "poor"
        report.summary = (
            f"Every chapter of this book scores about the same ({best:.0f} vs "
            f"{report.scores[0].runner_up:.0f}), so nothing actually matches."
        )
        report.warnings.append(
            "That pattern means a different book in the same language - the "
            "words are familiar but the story is not. Check you uploaded the "
            "right book and the right edition."
        )
    else:
        report.verdict = "poor"
        report.summary = (
            f"This text does not look like this audio ({best:.0f}, and this "
            f"book needs {report.accept_at:.0f} to count as a match)."
        )
