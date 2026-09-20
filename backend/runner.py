"""Runs one job: stage inputs, drive the aligner, collect the artifacts.

Nothing here knows which alignment backend is in use. The command to run, how
to read its progress and where it writes the subtitles all come from
`aligner.Aligner`, so swapping subplz out does not touch this file.

The backend is always a subprocess. That boundary is also what lets the public
deployment move alignment onto separate GPU workers without code changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

from . import languages, render
from .aligner import AlignRequest, aligner
from .db import Artifact, Job, JobStatus, SessionLocal, utcnow
from .settings import settings
from .storage import storage

log = logging.getLogger(__name__)

# Audio containers subplz/ffmpeg handle. Checked at upload time.
AUDIO_SUFFIXES = {
    ".m4b", ".m4a", ".mp3", ".opus", ".ogg", ".oga", ".flac", ".wav",
    ".aac", ".wma", ".mka", ".mkv", ".mp4", ".webm", ".avi", ".mov",
}
# Formats subplz reads directly, plus the ones convert.py turns into epub on
# the way in (fb2/mobi/azw3).
TEXT_SUFFIXES = {
    ".epub", ".txt", ".srt", ".vtt", ".ass",
    ".fb2", ".fb2.zip", ".mobi", ".azw", ".azw3", ".prc",
}


def text_suffix(name: str) -> str:
    """Suffix used when staging a text file, keeping the .fb2.zip double."""
    lowered = name.lower()
    if lowered.endswith(".fb2.zip"):
        return ".fb2.zip"
    return Path(lowered).suffix

# Fixed stem for staged inputs: keeps non-ASCII filenames out of the subprocess
# command line entirely, and makes the output path deterministic.
STAGE_STEM = "source"


class JobFailed(RuntimeError):
    pass


@dataclass
class Paths:
    root: Path
    inp: Path
    out: Path

    @classmethod
    def for_job(cls, job_id: str) -> "Paths":
        root = settings.data_dir / "work" / job_id
        return cls(root=root, inp=root / "input", out=root / "out")

    def create(self) -> None:
        self.inp.mkdir(parents=True, exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)


def input_prefix(job_id: str) -> str:
    """Storage prefix mirroring a job's staged `input/` tree.

    Only used when the queue is external: the worker that runs the job is
    probably not the machine that received the upload.
    """
    return f"{job_id}/input"


def ensure_inputs(job_id: str) -> None:
    """Make sure the staged inputs are on this machine's disk."""
    paths = Paths.for_job(job_id)
    paths.create()
    if any(p.is_file() for p in paths.inp.rglob("*")):
        return  # this process received the upload

    prefix = input_prefix(job_id)
    keys = storage.list_prefix(prefix)
    if not keys:
        raise JobFailed(
            "the uploaded files for this job are no longer available. "
            "Upload them again."
        )
    for key in keys:
        storage.fetch_to(key, paths.inp / key[len(prefix) + 1 :])


def staged_audio_path(job_id: str, original_name: str) -> Path:
    return Paths.for_job(job_id).inp / f"{STAGE_STEM}{Path(original_name).suffix.lower()}"


def staged_text_path(job_id: str, original_name: str) -> Path:
    return Paths.for_job(job_id).inp / f"{STAGE_STEM}{text_suffix(original_name)}"


def staged_cover_path(job_id: str, original_name: str) -> Path:
    """Where a user-supplied cover image is staged.

    Kept out of the `input/` root alongside audio and text so the aligner's
    directory scan cannot mistake a picture for content.
    """
    inp = Paths.for_job(job_id).inp
    return inp / "cover" / f"cover{Path(original_name).suffix.lower()}"


def find_cover(job_id: str) -> Path | None:
    folder = Paths.for_job(job_id).inp / "cover"
    if not folder.is_dir():
        return None
    return next((p for p in sorted(folder.iterdir()) if p.is_file()), None)


def staged_part_path(job_id: str, index: int, original_name: str) -> Path:
    """Where chapter file `index` (1-based) of a multi-part audiobook is staged.

    Parts get plain numeric names so the ffmpeg concat list never needs quoting
    and playback order is unambiguous.
    """
    inp = Paths.for_job(job_id).inp
    return inp / "parts" / f"{index:04d}{Path(original_name).suffix.lower()}"


# Tolerate damaged input rather than aborting the whole book. ffmpeg's default
# max_error_rate (0.667) kills a run over one bad chapter.
_FFMPEG_TOLERANT = [
    "-max_error_rate", "1.0",
    "-err_detect", "ignore_err",
    "-fflags", "+discardcorrupt",
]


def merge_parts(job_id: str, parts: list[Path]) -> Path:
    """Join a per-chapter audiobook into one file, one chapter per part.

    Keeping chapter marks matters twice over: subplz processes an m4b chapter by
    chapter, and the runner reads chapter completions to drive progress.
    """
    paths = Paths.for_job(job_id)
    dest = paths.inp / f"{STAGE_STEM}.m4b"
    if dest.exists():
        return dest

    durations: list[float] = []
    for p in parts:
        d = probe_duration(p)
        if d is None:
            raise JobFailed(f"could not read the duration of part {p.name}")
        durations.append(d)

    scratch = paths.root / "merge"
    scratch.mkdir(parents=True, exist_ok=True)

    listing = scratch / "parts.txt"
    # Absolute paths: ffmpeg resolves a relative entry against the directory of
    # the list file, not the working directory.
    listing.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
        encoding="utf-8",
    )

    # Chapter marks at the part boundaries, in milliseconds.
    meta = [";FFMETADATA1"]
    start_ms = 0
    for i, seconds in enumerate(durations, start=1):
        end_ms = start_ms + int(round(seconds * 1000))
        meta += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title=Part {i:02d}",
        ]
        start_ms = end_ms
    metadata = scratch / "chapters.txt"
    metadata.write_text("\n".join(meta) + "\n", encoding="utf-8")

    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        *_FFMPEG_TOLERANT,
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-i", str(metadata),
        "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1",
        # Drop cover art and any stray tracks the parts carry, so the only
        # media stream is the audio. (MP4 still writes a bin_data chapter
        # track; that one is how the container stores chapters and must stay.)
        "-vn", "-sn",
        # Keep listenable quality: the aligner downsamples to 16 kHz mono itself
        # when it reads the file, but the rendered video uses this same audio,
        # and 16 kHz mono would sound awful on YouTube.
        "-c:a", "aac", "-b:a", "128k",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=settings.job_timeout_seconds)
    if proc.returncode != 0 or not dest.exists():
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        tail = " | ".join(err[-3:]) if err else "no stderr"
        raise JobFailed(f"could not join the {len(parts)} audio parts: {tail}")

    shutil.rmtree(scratch, ignore_errors=True)
    return dest


def normalize_for_alignment(job_id: str, source: Path) -> Path:
    """Produce the 16 kHz mono copy the aligner gets fed.

    Two jobs at once:

    * Format. The acoustic model consumes 16 kHz mono no matter what, and
      handing subplz a 44.1 kHz stereo file has crashed ctranslate2 here
      (integer divide by zero, part-way through a chapter). Doing the
      conversion ourselves, once, keeps the aligner on the input that works.
    * Repair. The error-tolerant flags drop corrupt frames rather than letting
      ffmpeg abort the run, which is what a damaged chapter would otherwise do.

    The video keeps using `source`, which stays at listenable quality.
    """
    dest = Paths.for_job(job_id).inp / f"{STAGE_STEM}.align.m4a"
    if dest.exists():
        return dest

    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        *_FFMPEG_TOLERANT,
        "-i", str(source),
        "-map", "0:a:0", "-map_chapters", "0",
        "-vn", "-sn",
        "-c:a", "aac", "-b:a", "64k", "-ar", "16000", "-ac", "1",
        str(dest),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, timeout=settings.job_timeout_seconds
    )
    if proc.returncode != 0 or not dest.exists():
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        tail = " | ".join(err[-3:]) if err else "no stderr"
        raise JobFailed(f"could not prepare the audio for alignment: {tail}")
    return dest


def probe_chapters(path: Path) -> int:
    """Chapter count, or 1 for a flat file.

    subplz processes chaptered m4b files one chapter at a time, so this is what
    turns a stream of per-chapter progress bars into an overall percentage.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-print_format", "json",
             str(path)],
            capture_output=True, text=True, timeout=120, check=True,
        )
        return max(1, len(json.loads(out.stdout).get("chapters", [])))
    except (subprocess.SubprocessError, ValueError, OSError):
        return 1


def probe_duration(path: Path) -> float | None:
    """Audio length in seconds, via ffprobe. Used for progress and metadata."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=120, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError, FileNotFoundError, OSError):
        return None


def _staged(inp: Path, suffixes: set[str]) -> Path:
    """The one staged input with a suffix in `suffixes`."""
    for p in sorted(inp.iterdir()):
        if p.is_file() and p.suffix.lower() in suffixes:
            return p
    raise JobFailed(f"no staged input found with a supported extension in {inp.name}")


def build_request(job: Job, paths: Paths, audio: Path, chapters: int) -> AlignRequest:
    """Describe the job in backend-neutral terms."""
    languages.require(job.language)  # reject an unsupported code before we run
    return AlignRequest(
        audio=audio,
        text=_staged(paths.inp, TEXT_SUFFIXES),
        out_dir=paths.out,
        language=job.language,
        model=job.model,
        device=settings.device,
        threads=settings.threads or max(1, (os.cpu_count() or 4) - 1),
        chapters=chapters,
    )


def _set(job_id: str, **fields) -> None:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        s.commit()


def _is_canceled(job_id: str) -> bool:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        return job is not None and job.status == JobStatus.canceled


def run_job(job_id: str) -> None:
    """Execute one job end to end. Never raises; failures land on the Job row."""
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        if job.status == JobStatus.canceled:
            return
        audio_name, text_name = job.audio_filename, job.text_filename
        parts_count = job.audio_parts or 1

    paths = Paths.for_job(job_id)
    log_path = paths.root / "subplz.log"
    _set(job_id, status=JobStatus.running, started_at=utcnow(),
         stage="Preparing", progress=0.01)

    try:
        # No-op when this process received the upload; pulls from shared storage
        # when the job was enqueued on another machine.
        ensure_inputs(job_id)

        if parts_count > 1:
            _set(job_id, stage=f"Joining {parts_count} audio parts", progress=0.02)
            parts = sorted(p for p in (paths.inp / "parts").iterdir() if p.is_file())
            if len(parts) != parts_count:
                raise JobFailed(
                    f"expected {parts_count} audio parts but found {len(parts)}"
                )
            audio_in = merge_parts(job_id, parts)
        else:
            audio_in = staged_audio_path(job_id, audio_name)

        if not audio_in.exists():
            raise JobFailed(f"staged audio missing: {audio_in.name}")

        duration = probe_duration(audio_in)
        chapters = probe_chapters(audio_in)

        # The aligner gets a normalised 16 kHz mono copy; the video keeps the
        # full-quality one.
        _set(job_id, stage="Preparing audio", progress=0.03)
        align_audio = normalize_for_alignment(job_id, audio_in)

        _set(job_id, audio_duration_seconds=duration,
             stage=f"Starting {aligner.name}", progress=0.04)

        with SessionLocal() as s:
            job = s.get(Job, job_id)
            request = build_request(job, paths, align_audio, chapters)

        returncode = _stream_aligner(job_id, request, log_path)

        if _is_canceled(job_id):
            _cleanup_inputs(paths)
            return

        if returncode != 0:
            raise JobFailed(
                f"{aligner.name} exited with code {returncode}. "
                f"See the run log for details."
            )

        _set(job_id, stage="Collecting output", progress=0.92)
        _collect_artifacts(job_id, paths, log_path, duration, request, audio_in)

        _set(job_id, status=JobStatus.succeeded, stage="Done", progress=1.0,
             finished_at=utcnow())
        # Only on success: a failed job keeps its inputs so it can be retried
        # without re-uploading hundreds of megabytes.
        _cleanup_inputs(paths)

    except Exception as exc:  # noqa: BLE001 - surfaced to the user on the Job row
        if _is_canceled(job_id):
            _cleanup_inputs(paths)
            return
        # Keep the log even on failure: it is the only way to debug an alignment.
        try:
            _store_log(job_id, log_path)
        except Exception:
            pass
        _set(job_id, status=JobStatus.failed, error=str(exc)[:4000],
             stage="Failed", finished_at=utcnow())


def _stream_aligner(job_id: str, request: AlignRequest, log_path: Path) -> int:
    """Run the aligner, mirroring its output to a log file and to job progress.

    The backend decides what its output means; this only moves bytes and
    watches for cancellation and the time limit.
    """
    cmd = aligner.build_command(request)
    reader = aligner.progress_reader(request)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + settings.job_timeout_seconds

    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(cmd) + "\n\n")
        log.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=aligner.environment(),
            cwd=str(log_path.parent),
        )

        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                log.write(raw)
                log.flush()

                if time.monotonic() > deadline:
                    proc.kill()
                    raise JobFailed(
                        f"job exceeded the {settings.job_timeout_seconds}s time limit"
                    )

                if _is_canceled(job_id):
                    proc.kill()
                    return proc.wait()

                line = raw.strip()
                if not line:
                    continue

                update = reader.feed(line)
                if update is None:
                    continue
                fields: dict = {"stage": update.stage}
                if update.fraction is not None:
                    fields["progress"] = update.fraction
                _set(job_id, **fields)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

        return proc.returncode


def _collect_artifacts(job_id: str, paths: Paths, log_path: Path,
                       duration: float | None, request: AlignRequest,
                       video_audio: Path) -> None:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise JobFailed("job vanished while collecting output")
        language, model = job.language, job.model
        audio_name, text_name = job.audio_filename, job.text_filename
        splitter = job.splitter
        parts_count = job.audio_parts or 1

    produced = aligner.locate_output(request)
    if produced is None or not produced.exists():
        # The backend may exit 0 and still have failed, so ask it why before
        # falling back to guessing at the content.
        reason = aligner.failure_reason(request)
        if reason:
            raise JobFailed(f"{aligner.name} failed: {reason}")
        raise JobFailed(
            f"{aligner.name} finished but produced no subtitle file. The audio "
            "and text may not match, or the language may be wrong for this book."
        )

    # Give the download a filename the user will recognise. A per-chapter
    # audiobook has no single audio name worth using, so the book names it.
    stem = Path(text_name if parts_count > 1 else audio_name).stem
    download_name = f"{stem}.{language}{aligner.output_suffix}"

    cues, first_cue, last_cue = _summarize_srt(produced)

    srt_key = f"{job_id}/{download_name}"
    size = storage.put_file(srt_key, produced)

    # Three deliverables, because they serve three different jobs:
    #   .srt  - the timing file on its own (HoshiReader whispersync)
    #   .mp4  - clean video, no subtitle track, for YouTube (captions are
    #           uploaded separately there)
    #   .mkv  - the same video with the subtitles embedded, for local players
    # The mkv is a stream copy of the mp4, so the second file is nearly free.
    video_name = video_key = None
    embed_name = embed_key = None
    video_size = embed_size = 0

    if settings.render_video:
        try:
            _set(job_id, stage="Rendering video", progress=0.94)
            scratch = paths.root / "video"
            # A cover the user supplied wins over the one inside the epub.
            cover = find_cover(job_id) or render.extract_cover(request.text, scratch)
            canvas = render.build_canvas(cover, scratch / "canvas.png")

            video_name = f"{stem}.{language}.mp4"
            out_video = scratch / video_name
            render.render_video(
                audio=video_audio, canvas=canvas,
                dest=out_video, duration=duration,
            )
            video_key = f"{job_id}/{video_name}"
            video_size = storage.put_file(video_key, out_video)

            try:
                _set(job_id, stage="Embedding subtitles", progress=0.97)
                embed_name = f"{stem}.{language}.mkv"
                out_embed = scratch / embed_name
                render.mux_subtitles(out_video, produced, out_embed)
                embed_key = f"{job_id}/{embed_name}"
                embed_size = storage.put_file(embed_key, out_embed)
            except Exception as exc:  # noqa: BLE001
                embed_name = embed_key = None
                log.warning("job %s: subtitle embed failed: %s", job_id, exc)

        except Exception as exc:  # noqa: BLE001 - never fail a job over the video
            # The subtitles are the product, so a failed render is not fatal -
            # but it must not be silent either, or it looks like it never ran.
            video_name = video_key = None
            log.warning("job %s: video render failed: %s", job_id, exc,
                        exc_info=True)

    metadata = {
        "job_id": job_id,
        "created_at": utcnow().astimezone(timezone.utc).isoformat(),
        "source": {
            "audio_filename": audio_name,
            "audio_parts": parts_count,
            "text_filename": text_name,
            "audio_duration_seconds": duration,
        },
        "alignment": {
            "backend": aligner.name,
            "mode": "forced alignment against supplied text (nothing transcribed from scratch)",
            "model": model,
            "device": settings.device,
            "language": language,
            "language_name": (languages.get(language).name if languages.get(language) else language),
            "sentence_splitter": splitter,
        },
        "output": {
            "filename": download_name,
            "format": "srt",
            "cue_count": cues,
            "first_cue_start": first_cue,
            "last_cue_end": last_cue,
            "size_bytes": size,
        },
        "video": (
            {
                "filename": video_name,
                "container": "mp4",
                "subtitles": "none - upload the .srt to YouTube separately",
                "for": "youtube",
                "size_bytes": video_size,
            }
            if video_name
            else None
        ),
        "video_embedded": (
            {
                "filename": embed_name,
                "container": "mkv",
                "subtitles": "embedded SRT track, enabled by default",
                "for": "local playback (MPV, VLC, Jellyfin)",
                "size_bytes": embed_size,
            }
            if embed_name
            else None
        ),
    }
    meta_path = paths.root / "metadata.json"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    meta_key = f"{job_id}/metadata.json"
    meta_size = storage.put_file(meta_key, meta_path)

    rows = [
        Artifact(job_id=job_id, kind="srt", filename=download_name,
                 storage_key=srt_key, size_bytes=size),
        Artifact(job_id=job_id, kind="metadata", filename="metadata.json",
                 storage_key=meta_key, size_bytes=meta_size),
    ]
    if video_name and video_key:
        rows.append(
            Artifact(job_id=job_id, kind="video", filename=video_name,
                     storage_key=video_key, size_bytes=video_size)
        )
    if embed_name and embed_key:
        rows.append(
            Artifact(job_id=job_id, kind="video_embedded", filename=embed_name,
                     storage_key=embed_key, size_bytes=embed_size)
        )
    with SessionLocal() as s:
        for r in rows:
            s.add(r)
        s.commit()

    _store_log(job_id, log_path)


def _store_log(job_id: str, log_path: Path) -> None:
    if not log_path.exists():
        return
    with SessionLocal() as s:
        existing = (
            s.query(Artifact)
            .filter(Artifact.job_id == job_id, Artifact.kind == "log")
            .first()
        )
        if existing is not None:
            return
    key = f"{job_id}/subplz.log"
    size = storage.put_file(key, log_path)
    with SessionLocal() as s:
        s.add(Artifact(job_id=job_id, kind="log", filename="subplz.log",
                       storage_key=key, size_bytes=size))
        s.commit()


_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")


def _summarize_srt(path: Path) -> tuple[int, float | None, float | None]:
    """Cue count and span - cheap sanity signal that the alignment covered the book."""
    def to_seconds(m: re.Match) -> float:
        h, mi, s, ms = (int(g) for g in m.groups())
        return h * 3600 + mi * 60 + s + ms / 1000

    text = path.read_text(encoding="utf-8", errors="replace")
    stamps = list(_TIME.finditer(text))
    count = text.count(" --> ")
    if not stamps:
        return count, None, None
    return count, to_seconds(stamps[0]), to_seconds(stamps[-1])


def _cleanup_inputs(paths: Paths) -> None:
    """Drop the uploaded media once the run is over; keep artifacts and the log."""
    shutil.rmtree(paths.inp, ignore_errors=True)
    shutil.rmtree(paths.out, ignore_errors=True)
    shutil.rmtree(paths.root / "video", ignore_errors=True)
    # Also drop the shared-storage copy made for external workers.
    try:
        storage.delete_prefix(f"{paths.root.name}/input")
    except Exception:
        pass
