"""Make the licence key of Subrep Pro on the server, once.

    python tools/subrep_key.py

It writes SUBPLZ_WEB_SUBREP_LICENSE_KEY into .env and never shows it. It
prints the public key: build that into the Subrep desktop app (PUBLIC_KEY_B64
in subrep/licensing.py). When .env has the key already, it prints the public
key of that key and changes nothing. Every licence of every customer depends
on the key, so this tool never replaces it.

After the first run: systemctl restart subplz-web
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

NAME = "SUBPLZ_WEB_SUBREP_LICENSE_KEY"
ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def main() -> int:
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    found = [line.split("=", 1)[1].strip() for line in text.splitlines()
             if line.startswith(NAME + "=")]
    if found and found[-1]:
        key = Ed25519PrivateKey.from_private_bytes(b64d(found[-1]))
        print(f"{NAME} is set already. It stays as it is.", file=sys.stderr)
    else:
        key = Ed25519PrivateKey.generate()
        with ENV.open("a", encoding="utf-8") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write(f"{NAME}={b64e(key.private_bytes_raw())}\n")
        # As tools/set-secret.sh: the service does not run as root. The file
        # belongs to the owner of the checkout, and nobody else may read it.
        if hasattr(os, "chown"):
            owner = ROOT.stat()
            os.chown(ENV, owner.st_uid, owner.st_gid)
        os.chmod(ENV, 0o600)
        print(f"{NAME} saved in {ENV}. Apply it with: systemctl restart subplz-web",
              file=sys.stderr)
    print(b64e(key.public_key().public_bytes_raw()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
