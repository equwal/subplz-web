"""Download the browser-side libraries and the speech model into frontend/vendor/.

Processing happens in the visitor's browser, which needs four things that are
far too big to commit: ffmpeg compiled to WebAssembly (reads any audio format,
muxes the video), the ONNX runtime, transformers.js to drive Whisper on it, and
the Whisper-tiny weights themselves. They are pinned here by exact version or
hash, taken from the npm registry and the Hugging Face hub, then served from
our own origin - no CDN and no third-party host in the page, so nothing outside
has to be trusted or kept alive, and cross-origin isolation stays simple.

    python tools/fetch_vendor.py          # idempotent; run on every deploy

Standard library only, on purpose: this runs on the server before anything else
is installed.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "frontend" / "vendor"

# package, version, {path inside the tarball: path under vendor/}
PACKAGES = [
    ("@ffmpeg/ffmpeg", "0.12.15", {
        f"package/dist/esm/{name}": f"ffmpeg/{name}"
        for name in ("index.js", "classes.js", "const.js", "errors.js", "types.js", "utils.js", "worker.js")
    }),
    ("@ffmpeg/core", "0.12.10", {
        "package/dist/esm/ffmpeg-core.js": "ffmpeg/ffmpeg-core.js",
        "package/dist/esm/ffmpeg-core.wasm": "ffmpeg/ffmpeg-core.wasm",
    }),
    ("@huggingface/transformers", "4.3.0", {
        "package/dist/transformers.min.js": "transformers/transformers.min.js",
    }),
    # Must be exactly the build transformers.js was made against.
    ("onnxruntime-web", "1.31.0-dev.20260914-8d85527a0", {
        f"package/dist/{name}": f"transformers/{name}"
        for name in ("ort.webgpu.bundle.min.mjs", "ort-wasm-simd-threaded.asyncify.mjs",
                     "ort-wasm-simd-threaded.asyncify.wasm")
    }),
    # Reads Kindle books (MOBI and KF8) in the browser. One file, no imports.
    ("foliate-js", "1.0.1", {
        "package/mobi.js": "foliate/mobi.js",
    }),
]


# The speech model, as transformers.js loads it: the config and tokenizer files,
# the full-precision encoder, and one decoder for each device (4-bit on WebGPU,
# 8-bit on WebAssembly). The hash is the sha256 of the file on the hub, so a
# changed or truncated download stops here.
MODEL_REPO = "onnx-community/whisper-tiny"
MODEL_REVISION = "main"
MODEL_DIR = "models/whisper-tiny"
MODEL_FILES = {
    "config.json": None,
    "preprocessor_config.json": None,
    "tokenizer_config.json": None,
    "generation_config.json": None,
    "tokenizer.json": None,
    "onnx/encoder_model.onnx": "6642befb640f950d",
    "onnx/decoder_model_merged_q4.onnx": "a7573efde84f7d01",
    "onnx/decoder_model_merged_quantized.onnx": "25e807a962b63493",
}


def fetch(package: str, version: str) -> bytes:
    meta_url = f"https://registry.npmjs.org/{package.replace('/', '%2F')}/{version}"
    with urllib.request.urlopen(meta_url, timeout=60) as r:
        dist = json.load(r)["dist"]
    with urllib.request.urlopen(dist["tarball"], timeout=600) as r:
        blob = r.read()
    # The registry's own checksum: a tampered or truncated download stops here.
    if hashlib.sha1(blob).hexdigest() != dist["shasum"]:
        sys.exit(f"{package}@{version}: checksum mismatch")
    return blob


def fetch_model() -> None:
    """The Whisper weights, once. A file whose hash matches is not fetched again."""
    for name, prefix in MODEL_FILES.items():
        out = VENDOR / MODEL_DIR / name
        if out.exists() and (prefix is None or hashlib.sha256(out.read_bytes()).hexdigest().startswith(prefix)):
            print(f"ok       {MODEL_REPO}/{name}")
            continue
        print(f"fetching {MODEL_REPO}/{name}")
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
        with urllib.request.urlopen(url, timeout=600) as r:
            blob = r.read()
        if prefix is not None and not hashlib.sha256(blob).hexdigest().startswith(prefix):
            sys.exit(f"{MODEL_REPO}/{name}: checksum mismatch")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(blob)


def main() -> None:
    stamp = VENDOR / "versions.json"
    want = {p: v for p, v, _ in PACKAGES}
    have = json.loads(stamp.read_text()) if stamp.exists() else {}

    for package, version, files in PACKAGES:
        targets = [VENDOR / dest for dest in files.values()]
        if have.get(package) == version and all(t.exists() for t in targets):
            print(f"ok       {package}@{version}")
            continue
        print(f"fetching {package}@{version}")
        with tarfile.open(fileobj=io.BytesIO(fetch(package, version)), mode="r:gz") as tar:
            for src, dest in files.items():
                member = tar.extractfile(src)
                if member is None:
                    sys.exit(f"{package}@{version}: {src} is not in the package")
                out = VENDOR / dest
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(member.read())

    stamp.write_text(json.dumps(want, indent=2))
    fetch_model()
    total = sum(f.stat().st_size for f in VENDOR.rglob("*") if f.is_file())
    print(f"vendor/ is {total / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
