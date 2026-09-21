"""probe_chapters must never fail a job: the chapter count only scales a progress bar."""

from __future__ import annotations

import subprocess
from pathlib import Path

from backend import runner


def test_no_output_from_ffprobe_counts_as_one_chapter(monkeypatch):
    # On a Windows machine with a non-UTF-8 code page, the reader thread of
    # subprocess fails to decode ffprobe's output and leaves stdout as None.
    # json.loads(None) raises TypeError, which failed the whole job:
    # "the JSON object must be str, bytes or bytearray, not NoneType".
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=None, stderr=None),
    )
    assert runner.probe_chapters(Path("book.mp3")) == 1


def test_chapters_are_counted(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout='{"chapters": [{}, {}, {}]}', stderr=""),
    )
    assert runner.probe_chapters(Path("book.m4b")) == 3
