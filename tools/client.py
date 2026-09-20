"""Command-line client for the SubPlz web API.

Useful on its own for batch work, and it doubles as a worked example of the
three-call flow the browser uses.

    python tools/client.py submit "book.m4b" "book.epub" [--language ru] [--wait]
    python tools/client.py list
    python tools/client.py get <job_id>
    python tools/client.py download <job_id> [--dir out/]

Uses only the standard library so it runs anywhere, and sends multipart bodies
itself so non-ASCII filenames survive (curl on a non-UTF-8 console mangles them).
"""

from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

DEFAULT_BASE = os.environ.get("SUBPLZ_WEB_URL", "http://127.0.0.1:8420")
COOKIE_FILE = Path.home() / ".subplz_web_cookie"


class Client:
    def __init__(self, base: str):
        parts = urllib.parse.urlsplit(base)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.https = parts.scheme == "https"
        self.cookie = COOKIE_FILE.read_text().strip() if COOKIE_FILE.exists() else ""

    def _conn(self):
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=900)

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(extra or {})
        return h

    def _remember_cookie(self, resp) -> None:
        raw = resp.getheader("set-cookie")
        if raw:
            self.cookie = raw.split(";", 1)[0]
            try:
                COOKIE_FILE.write_text(self.cookie)
            except OSError:
                pass

    def request(self, method: str, path: str, body=None, headers=None):
        conn = self._conn()
        conn.request(method, path, body, self._headers(headers))
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        self._remember_cookie(resp)
        conn.close()

        if resp.status >= 400:
            try:
                detail = json.loads(raw).get("detail", raw)
            except json.JSONDecodeError:
                detail = raw[:500]
            raise SystemExit(f"error {resp.status}: {detail}")
        return json.loads(raw) if raw else None

    def upload(self, audio: list[Path], text: Path) -> dict:
        boundary = "----subplz" + uuid.uuid4().hex
        body = bytearray()
        for path in [*audio, text]:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            disposition = (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="files"; '
                f'filename="{path.name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            )
            # The header is encoded as UTF-8, which is what browsers send and
            # what Starlette decodes - this is the bit curl gets wrong.
            body += disposition.encode("utf-8")
            body += path.read_bytes()
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()

        return self.request(
            "POST", "/api/uploads", bytes(body),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def start(self, job_id: str, language: str | None) -> dict:
        payload = json.dumps({"language": language} if language else {})
        return self.request(
            "POST", f"/api/jobs/{job_id}/start",
            payload.encode(), {"Content-Type": "application/json"},
        )

    def get(self, job_id: str) -> dict:
        return self.request("GET", f"/api/jobs/{job_id}")

    def list(self) -> list:
        return self.request("GET", "/api/jobs")

    def download(self, job_id: str, kind: str, out_dir: Path) -> Path | None:
        job = self.get(job_id)
        art = next((a for a in job["artifacts"] if a["kind"] == kind), None)
        if art is None:
            return None
        conn = self._conn()
        conn.request("GET", art["url"], None, self._headers())
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status >= 400:
            raise SystemExit(f"download failed: {resp.status}")
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / art["filename"]
        dest.write_bytes(data)
        return dest


def _fmt(job: dict) -> str:
    pct = int(job["progress"] * 100)
    return (
        f"{job['id']}  {job['status']:<10} {pct:>3}%  "
        f"{job['stage']:<34} {job['language_name']}  {job['audio_filename']}"
    )


def wait(client: Client, job_id: str) -> dict:
    last = None
    while True:
        job = client.get(job_id)
        line = _fmt(job)
        if line != last:
            print(line, flush=True)
            last = line
        if job["status"] in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit", help="upload a pair and start aligning")
    s.add_argument("audio")
    s.add_argument("text")
    s.add_argument("--language", help="override the detected language")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--dir", default="subplz-out", help="where to save on --wait")

    sub.add_parser("list", help="list your jobs")

    g = sub.add_parser("get", help="show one job")
    g.add_argument("job_id")

    w = sub.add_parser("watch", help="follow a job to completion")
    w.add_argument("job_id")

    d = sub.add_parser("download", help="save a finished job's files")
    d.add_argument("job_id")
    d.add_argument("--dir", default="subplz-out")

    args = ap.parse_args()
    client = Client(args.base)

    if args.cmd == "submit":
        audio_arg, text = Path(args.audio), Path(args.text)
        if not text.exists():
            raise SystemExit(f"no such file: {text}")

        if audio_arg.is_dir():
            # A folder of per-chapter files. Order is settled server-side.
            exts = {".m4b", ".m4a", ".mp3", ".opus", ".ogg", ".oga", ".flac",
                    ".wav", ".aac", ".wma", ".mka", ".mkv", ".mp4", ".webm"}
            audio = sorted(
                p for p in audio_arg.iterdir()
                if p.is_file() and p.suffix.lower() in exts
            )
            if not audio:
                raise SystemExit(f"no audio files in {audio_arg}")
        else:
            if not audio_arg.exists():
                raise SystemExit(f"no such file: {audio_arg}")
            audio = [audio_arg]

        size_mb = (sum(p.stat().st_size for p in audio) + text.stat().st_size) / 1024**2
        label = f"{len(audio)} audio files" if len(audio) > 1 else audio[0].name
        print(f"uploading {label}, {size_mb:.0f} MB ...", flush=True)
        res = client.upload(audio, text)

        job, det = res["job"], res["detected"]
        print(f"paired: audio={job['audio_filename']} text={job['text_filename']}")
        if det["code"]:
            print(
                f"detected language: {det['name']} ({det['code']}) "
                f"{det['confidence'] * 100:.1f}% confident"
                + ("" if det["supported"] else "  [NOT SUPPORTED - pass --language]")
            )
        lang = args.language or job["language"]
        print(f"starting with language={lang}")
        started = client.start(job["id"], args.language)
        print(_fmt(started))

        if args.wait:
            final = wait(client, job["id"])
            if final["status"] == "succeeded":
                for kind in ("srt", "metadata"):
                    dest = client.download(job["id"], kind, Path(args.dir))
                    if dest:
                        print(f"saved {dest}")
                return 0
            print(f"job {final['status']}: {final.get('error') or ''}")
            return 1
        return 0

    if args.cmd == "list":
        jobs = client.list()
        if not jobs:
            print("no jobs")
        for j in jobs:
            print(_fmt(j))
        return 0

    if args.cmd == "get":
        print(json.dumps(client.get(args.job_id), indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "watch":
        final = wait(client, args.job_id)
        return 0 if final["status"] == "succeeded" else 1

    if args.cmd == "download":
        any_saved = False
        for kind in ("srt", "metadata", "log"):
            dest = client.download(args.job_id, kind, Path(args.dir))
            if dest:
                print(f"saved {dest}")
                any_saved = True
        if not any_saved:
            print("nothing to download yet")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
