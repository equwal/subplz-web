"""Render a YouTube-ready MP4: cover image, the audio, and a soft subtitle track.

The subtitles are a selectable track rather than burned into the picture, so the
file stays small, the encode stays fast, and the viewer can turn captions off.
YouTube reads the track on upload; the .srt is also offered on its own for
people who would rather attach it there.

The encode is cheap by construction: one still frame per second over a canvas
that is scaled once up front, so ffmpeg never recomputes the scale per frame.
"""

from __future__ import annotations

import logging
import subprocess
import zipfile
from functools import lru_cache
from pathlib import Path

from .settings import settings

log = logging.getLogger(__name__)

# Cover art we are willing to pull out of an epub.
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

# H.264 encoders in order of preference. ffmpeg builds vary wildly in what they
# include - libx264 is the usual default but is absent from plenty of builds,
# so pick from what is actually compiled in rather than assuming.
_H264_PREFERENCE = [
    "libx264",      # best quality per bit, most common
    "h264_nvenc",   # NVIDIA
    "h264_qsv",     # Intel Quick Sync
    "h264_amf",     # AMD
    "libopenh264",  # software fallback, always safe
    "h264_mf",      # Windows Media Foundation
]

# Encoder-specific quality flags. A libx264 -crf/-preset means nothing to the
# others and makes ffmpeg fail outright.
_ENCODER_FLAGS: dict[str, list[str]] = {
    "libx264": ["-preset", "veryfast", "-tune", "stillimage"],
    "libopenh264": ["-b:v", "1M"],
    "h264_nvenc": ["-preset", "p4", "-tune", "ll"],
    "h264_qsv": ["-preset", "veryfast"],
    "h264_amf": ["-quality", "speed", "-rc", "cqp"],
    "h264_mf": [],
}


class RenderError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def available_encoders() -> set[str]:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, timeout=60,
        ).stdout.decode("utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        return set()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        # Rows look like " V....D libopenh264  OpenH264 ..." - the name is [1].
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def encoder_works(name: str) -> bool:
    """Actually encode one frame with `name`.

    Being listed by `ffmpeg -encoders` only means it was compiled in. The
    hardware encoders are listed on machines with no such hardware and fail at
    runtime, so the only trustworthy check is to run one.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1",
        "-frames:v", "1", "-c:v", name,
        *_ENCODER_FLAGS.get(name, []),
        "-pix_fmt", "yuv420p", "-f", "null", "-",
    ]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


@lru_cache(maxsize=1)
def resolve_encoder() -> str:
    """The H.264 encoder to use, honouring an explicit setting if it works."""
    have = available_encoders()
    configured = settings.video_encoder

    if configured and configured != "auto":
        if encoder_works(configured):
            return configured
        log.warning(
            "video_encoder=%r does not work on this machine; falling back",
            configured,
        )

    for name in _H264_PREFERENCE:
        if name in have and encoder_works(name):
            log.info("using video encoder %s", name)
            return name

    raise RenderError(
        "No working H.264 encoder found (tried "
        + ", ".join(n for n in _H264_PREFERENCE if n in have)
        + "). Install an ffmpeg with libx264 or libopenh264, or set "
        "SUBPLZ_WEB_RENDER_VIDEO=false."
    )


def extract_cover(book: Path, dest_dir: Path) -> Path | None:
    """Pull the largest image out of an epub, as a stand-in for cover art.

    "Largest" beats parsing the OPF metadata for this purpose: the cover is
    almost always the biggest image, and this still works on the malformed
    epubs that converted books often are.
    """
    if book.suffix.lower() != ".epub":
        return None
    try:
        with zipfile.ZipFile(book) as zf:
            images = [
                info for info in zf.infolist()
                if Path(info.filename).suffix.lower() in _IMAGE_SUFFIXES
            ]
            if not images:
                return None
            best = max(images, key=lambda i: i.file_size)
            if best.file_size < 1024:  # a bullet or a rule, not a cover
                return None
            dest = dest_dir / f"cover{Path(best.filename).suffix.lower()}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(best.filename))
            return dest
    except (zipfile.BadZipFile, OSError, KeyError):
        return None


def build_canvas(cover: Path | None, dest: Path) -> Path:
    """Scale the cover onto a fixed canvas once, ahead of the encode."""
    w, h = settings.video_width, settings.video_height
    dest.parent.mkdir(parents=True, exist_ok=True)

    if cover is not None and cover.exists():
        cmd = [
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-i", str(cover),
            "-vf",
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
            "-frames:v", "1",
            str(dest),
        ]
    else:
        # No cover: a plain dark card still gives YouTube a valid video stream.
        cmd = [
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=0x16161a:s={w}x{h}",
            "-frames:v", "1",
            str(dest),
        ]

    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0 or not dest.exists():
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RenderError(
            "could not prepare the cover image: "
            + (" | ".join(err[-2:]) if err else "no output")
        )
    return dest


def render_video(
    audio: Path,
    canvas: Path,
    dest: Path,
    duration: float | None = None,
) -> Path:
    """Cover image + audio into a clean MP4, carrying no subtitle track.

    Subtitle-free on purpose. This is the file for YouTube, where captions are
    attached separately as an .srt: YouTube's handling of an embedded mov_text
    track is unreliable, and uploading the .srt is the supported route.

    This is the expensive step - `mux_subtitles` reuses its output instead of
    encoding a second time.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-loop", "1", "-r", str(settings.video_fps), "-i", str(canvas),
        "-i", str(audio),
    ]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    else:
        cmd += ["-shortest"]

    encoder = resolve_encoder()
    cmd += [
        "-map", "0:v", "-map", "1:a",
        # Carry chapter marks through; harmless where they are ignored.
        "-map_chapters", "1",
        "-c:v", encoder,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        # Lets a player start without reading the whole file first.
        "-movflags", "+faststart",
    ]
    cmd += _ENCODER_FLAGS.get(encoder, [])
    # -crf is a libx264/libx265 concept; the others have their own rate control.
    if encoder == "libx264":
        cmd += ["-crf", str(settings.video_crf)]

    cmd.append(str(dest))

    proc = subprocess.run(
        cmd, capture_output=True, timeout=settings.job_timeout_seconds
    )
    if proc.returncode != 0 or not dest.exists():
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        tail = " | ".join(err[-3:]) if err else "no output"
        raise RenderError(f"ffmpeg could not build the video: {tail}")
    return dest


def mux_subtitles(video: Path, subtitles: Path, dest: Path) -> Path:
    """Copy `video` into an MKV carrying `subtitles` as a selectable track.

    For local playback - MPV, VLC, Jellyfin - where one self-contained file
    beats juggling a video and a sidecar .srt.

    MKV rather than MP4 because MKV stores SRT natively, keeping the cue text
    intact; MP4 must convert to mov_text, which is a lossy reduction. Nothing
    is re-encoded here: the streams are copied, so this costs seconds however
    long the book is.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-i", str(video),
        # Declare the format; ffmpeg does not always sniff an srt correctly.
        "-f", "srt", "-i", str(subtitles),
        # Map video and audio explicitly rather than "-map 0": an MP4 carries
        # its chapters as a bin_data track, and Matroska accepts only audio,
        # video and subtitle streams - it refuses the file outright otherwise.
        # Chapters still survive via -map_chapters, being container metadata.
        "-map", "0:v", "-map", "0:a", "-map", "1:s",
        "-map_chapters", "0",
        "-c", "copy", "-c:s", "srt",
        # Players pick this up automatically instead of needing it turned on.
        "-disposition:s:0", "default",
        "-metadata:s:s:0", "title=Aligned subtitles",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=1800)
    if proc.returncode != 0 or not dest.exists():
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        tail = " | ".join(err[-3:]) if err else "no output"
        raise RenderError(f"ffmpeg could not embed the subtitles: {tail}")
    return dest
