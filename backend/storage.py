"""Artifact storage. Local filesystem now, S3 for the public release.

Everything downstream deals in opaque storage keys, so swapping the backend
touches neither the API nor the job runner.
"""

import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from .settings import settings


class Storage(ABC):
    @abstractmethod
    def put_file(self, key: str, source: Path) -> int:
        """Store the file at `source` under `key`; return the byte size."""

    @abstractmethod
    def open_stream(self, key: str):
        """Return a binary file-like object for `key`."""

    @abstractmethod
    def read_text(self, key: str) -> str:
        ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        ...

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """Every key under `prefix`. Used to move a job's inputs between machines."""

    def presigned_url(self, key: str, filename: str) -> str | None:
        """S3 hands back a direct URL; local storage streams through the app."""
        return None

    def exists(self, key: str) -> bool:
        try:
            self.open_stream(key).close()
            return True
        except Exception:
            return False

    def fetch_to(self, key: str, dest: Path) -> Path:
        """Copy `key` out of storage onto local disk.

        This is what lets a worker on another machine pick up a job whose files
        were uploaded to a different API process.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = self.open_stream(key)
        try:
            with dest.open("wb") as out:
                while chunk := src.read(8 * 1024 * 1024):
                    out.write(chunk)
        finally:
            close = getattr(src, "close", None)
            if close:
                close()
        return dest


class LocalStorage(Storage):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are app-generated, but refuse traversal regardless.
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root)):
            raise ValueError(f"unsafe storage key: {key!r}")
        return p

    def path_for(self, key: str) -> Path:
        """On-disk location of `key`. Local backend only - used to serve files."""
        return self._path(key)

    def put_file(self, key: str, source: Path) -> int:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        return dest.stat().st_size

    def open_stream(self, key: str):
        return self._path(key).open("rb")

    def read_text(self, key: str) -> str:
        return self._path(key).read_text(encoding="utf-8")

    def delete_prefix(self, prefix: str) -> None:
        target = self._path(prefix)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()

    def list_prefix(self, prefix: str) -> list[str]:
        target = self._path(prefix)
        if not target.is_dir():
            return [prefix] if target.exists() else []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in target.rglob("*")
            if p.is_file()
        )


class S3Storage(Storage):
    """Public-release backend. Needs boto3 and SUBPLZ_WEB_S3_BUCKET."""

    def __init__(self, bucket: str, prefix: str):
        import boto3  # lazy import so localhost never needs boto3

        self.client = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put_file(self, key: str, source: Path) -> int:
        self.client.upload_file(str(source), self.bucket, self._key(key))
        return source.stat().st_size

    def open_stream(self, key: str):
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"]

    def read_text(self, key: str) -> str:
        return self.open_stream(key).read().decode("utf-8")

    def delete_prefix(self, prefix: str) -> None:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})

    def list_prefix(self, prefix: str) -> list[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        out: list[str] = []
        head = len(self.prefix)
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            out += [o["Key"][head:] for o in page.get("Contents", [])]
        return sorted(out)

    def presigned_url(self, key: str, filename: str) -> str | None:
        disposition = f'attachment; filename="{filename}"'
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": self._key(key),
                "ResponseContentDisposition": disposition,
            },
            ExpiresIn=settings.download_url_ttl_seconds,
        )


def get_storage() -> Storage:
    if settings.storage_backend == "s3":
        if not settings.s3_bucket:
            raise RuntimeError("storage_backend=s3 requires SUBPLZ_WEB_S3_BUCKET")
        return S3Storage(settings.s3_bucket, settings.s3_prefix)
    return LocalStorage(settings.data_dir / "artifacts")


storage = get_storage()
