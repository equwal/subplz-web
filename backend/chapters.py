"""Chapter matching that works on any script.

The alignment backend decides which text chapter belongs to which audio chapter
by character-level edit similarity (`rapidfuzz.fuzz.ratio`) against a fixed
threshold of 40. That is calibrated for Japanese and does not transfer. Measured
noise floor between *unrelated* chapters of the same book:

    Japanese   23-26      <- threshold 40 discriminates well
    Russian    39         <- borderline
    Spanish    44
    English    45-47      <- unrelated chapters clear the threshold

With twenty-six letters, any two prose texts are ~45% similar by chance, so for
Latin scripts the gate is *below the noise* and accepts anything. Cleaning does
not fix it (a generic punctuation strip moved English only 45.3 -> 42.4); the
cause is alphabet size, not typography.

This module scores on character n-gram overlap instead, and calibrates the
threshold against the book it is actually looking at. Measured on the same data:

    metric            ru floor  en floor  ja floor   true   runner-up   ratio
    fuzz.ratio            39.2      45.1      26.1   86.3        40.8   2.1x
    word jaccard          10.4      17.7       1.9   63.2        14.8   4.3x
    4-gram jaccard         4.8      10.6       2.5   53.3         5.6   9.5x

n-grams rather than words because plenty of languages do not put spaces between
them; a word-level metric collapses on Japanese, where a "word" is a whole run of
characters.
"""

from __future__ import annotations

import random
import statistics as st
import unicodedata
from dataclasses import dataclass

import regex

NGRAM = 4

# Everything that is not a letter or a number: punctuation, spacing, marks.
# Dropped so typography cannot influence the comparison.
_NOISE = regex.compile(r"[^\p{L}\p{N}]+")


def normalize(text: str) -> str:
    return _NOISE.sub("", unicodedata.normalize("NFKD", text.casefold()))


def fingerprint(text: str, n: int = NGRAM) -> frozenset[str]:
    """The set of character n-grams in `text`, after normalisation.

    Computed once per chapter and reused: comparing two fingerprints is a set
    intersection, which is far cheaper than an edit distance over the same text.
    """
    s = normalize(text)
    if len(s) < n:
        return frozenset()
    return frozenset(s[i : i + n] for i in range(len(s) - n + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return 100.0 * len(a & b) / len(a | b)


def containment(probe: frozenset[str], reference: frozenset[str]) -> float:
    """How much of `probe` appears in `reference`, as a percentage.

    Used when a short transcript sample is compared against a whole chapter:
    Jaccard would punish the length difference through the union term, since a
    three-minute sample cannot cover a thirty-minute chapter. Containment asks
    the question we actually mean - "is this passage in that chapter" - and is
    unaffected by how much longer the chapter is.
    """
    if not probe or not reference:
        return 0.0
    return 100.0 * len(probe & reference) / len(probe)


@dataclass(frozen=True)
class Calibration:
    """The similarity this book produces by chance.

    Sampled from the book itself, so it adapts to script, vocabulary and
    register instead of relying on a constant that only suits one language.
    """

    floor: float
    high: float  # worst case seen among unrelated pairs
    samples: int

    # A real match must clear the floor by this factor...
    FLOOR_MULTIPLE = 1.6
    # ...and beat whatever came second by this much. This is the test a fixed
    # threshold cannot do, and the one that catches a wrong book whose language
    # alone makes every chapter score alike.
    RUNNER_UP_MULTIPLE = 1.5

    @property
    def accept_at(self) -> float:
        """Minimum score worth taking seriously for this book."""
        return max(self.floor * self.FLOOR_MULTIPLE, self.high * 1.15, 1.0)


def calibrate(
    chapter_texts: list[str],
    pairs: int = 150,
    seed: int = 0,
    probe_chars: int = 2000,
) -> Calibration:
    """Estimate the chance-similarity floor from unrelated pairs of this book.

    Calibrated with the *same* measure and the *same shape* of comparison that
    real matching uses: a short probe against a whole chapter. Measuring the
    floor with Jaccard while matching with containment would put the threshold
    on a different scale from the scores it gates.
    """
    usable = [t for t in chapter_texts if len(normalize(t)) > probe_chars // 2]
    if len(usable) < 3:
        # Too little to calibrate against; fall back to something conservative.
        return Calibration(floor=20.0, high=40.0, samples=0)

    references = [fingerprint(t) for t in usable]
    # A probe is a slice the size of a transcript sample, so the floor reflects
    # what an unrelated passage of this length scores against a full chapter.
    probes = [fingerprint(normalize(t)[:probe_chars]) for t in usable]

    rng = random.Random(seed)
    scores = []
    for _ in range(pairs):
        a, b = rng.sample(range(len(usable)), 2)
        scores.append(containment(probes[a], references[b]))

    scores.sort()
    return Calibration(
        floor=st.median(scores),
        high=scores[min(len(scores) - 1, int(0.95 * len(scores)))],
        samples=len(scores),
    )


@dataclass(frozen=True)
class Match:
    text_index: int | None
    score: float
    runner_up: float
    accepted: bool
    calibration: Calibration

    @property
    def confidence(self) -> float:
        """How far clear of the runner-up this match is. 1.0 means a tie."""
        if self.runner_up <= 0:
            return float("inf") if self.score > 0 else 0.0
        return self.score / self.runner_up

    @property
    def margin_over_noise(self) -> float:
        if self.calibration.floor <= 0:
            return float("inf") if self.score > 0 else 0.0
        return self.score / self.calibration.floor


def best_match(
    probe: frozenset[str],
    chapters: list[frozenset[str]],
    calibration: Calibration,
    exclude: set[int] | None = None,
) -> Match:
    """Find the chapter a sample of audio came from.

    Accepting requires clearing the book's own noise floor *and* beating the
    runner-up. The second test is what a fixed threshold cannot do: if two
    chapters score alike, the winner is arbitrary, however high the number.
    """
    skip = exclude or set()
    ranked = sorted(
        (
            (containment(probe, ch), i)
            for i, ch in enumerate(chapters)
            if i not in skip and ch
        ),
        reverse=True,
    )
    if not ranked:
        return Match(None, 0.0, 0.0, False, calibration)

    score, index = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0

    accepted = score >= calibration.accept_at and (
        runner_up <= 0 or score >= runner_up * Calibration.RUNNER_UP_MULTIPLE
    )
    return Match(index if accepted else None, score, runner_up, accepted, calibration)


def assign_monotonic(matrix: list[list[float]], accept_at: float) -> list[int | None]:
    """Pair audio chapters to text chapters in order, maximising total score.

    The backend's own matcher walks audio chapters in order and *consumes* text
    chapters as it goes, with no backtracking: an early chapter - usually
    chapter 0, where publisher announcements live and which is therefore the
    least representative chapter in the book - can permanently take the text
    that belonged to another.

    This solves the whole assignment at once, and requires it to be
    order-preserving, which is true of every book: chapter k cannot come from
    text that precedes chapter k-1's. Skips are allowed on both sides, for front
    matter and for chapters nobody narrated.
    """
    n_audio, n_text = len(matrix), (len(matrix[0]) if matrix else 0)
    if not n_audio or not n_text:
        return [None] * n_audio

    NEG = float("-inf")
    # best[i][j] = best total score pairing the first i audio with first j text
    best = [[0.0] * (n_text + 1) for _ in range(n_audio + 1)]
    back = [[0] * (n_text + 1) for _ in range(n_audio + 1)]  # 0 skip-a,1 skip-t,2 pair

    for i in range(1, n_audio + 1):
        for j in range(1, n_text + 1):
            skip_audio = best[i - 1][j]
            skip_text = best[i][j - 1]
            score = matrix[i - 1][j - 1]
            pair = best[i - 1][j - 1] + (score if score >= accept_at else NEG)

            chosen = max(skip_audio, skip_text, pair)
            best[i][j] = chosen
            back[i][j] = 2 if chosen == pair else (0 if chosen == skip_audio else 1)

    out: list[int | None] = [None] * n_audio
    i, j = n_audio, n_text
    while i > 0 and j > 0:
        move = back[i][j]
        if move == 2:
            out[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif move == 0:
            i -= 1
        else:
            j -= 1
    return out
