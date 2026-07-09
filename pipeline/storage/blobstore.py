"""Blob store interface — the ONLY cloud-specific abstraction (plan §14).

`local` is a directory on disk; `s3` is a bucket+prefix. Both are content-
addressed: the caller hands over a key like ``sha256/ab/cd/<hash>/artifact.md``
and the store maps it to a path or object. Selected in ``pipeline.yaml``; the
rest of the system never knows which is active.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.config import Settings


class BlobStore(ABC):
    @abstractmethod
    def write(self, key: str, data: bytes) -> str:
        """Store bytes at key; return a locator (path or s3:// URL)."""

    @abstractmethod
    def read(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def locator(self, key: str) -> str:
        """Stable reference to the object (for manifests / logs)."""


class LocalBlobStore(BlobStore):
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    def write(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def locator(self, key: str) -> str:
        return str(self._path(key))


class S3BlobStore(BlobStore):
    def __init__(self, bucket: str, prefix: str = "", region: str | None = None):
        import boto3  # imported lazily so local mode needs no AWS deps at runtime

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = boto3.client("s3", region_name=region)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def write(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
        return self.locator(key)

    def read(self, key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return obj["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False

    def locator(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"


def get_blobstore(settings: "Settings") -> BlobStore:
    cfg = settings.raw["storage"]
    backend = cfg.get("blobstore", "local")
    if backend == "local":
        root = Path(cfg["local"]["root"])
        if not root.is_absolute():
            root = settings.root / root
        return LocalBlobStore(root)
    if backend == "s3":
        s3 = cfg["s3"]
        return S3BlobStore(s3["bucket"], s3.get("prefix", ""), s3.get("region"))
    raise ValueError(f"unknown blobstore backend: {backend!r}")
