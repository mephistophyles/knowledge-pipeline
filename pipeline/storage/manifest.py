"""Content addressing + manifest writing (plan §4).

Every artifact lands in the raw store as ``<hash>/artifact.<ext>`` alongside a
``manifest.json`` recording its provenance. ``annotates`` is set when the
artifact is personal notes *about* another artifact (plan §4.7).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from pipeline.storage.blobstore import BlobStore


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_key(hash_: str, filename: str) -> str:
    # sha256/ab/cd/<full-hash>/<filename>  — mirrors the local + S3 layout in plan §2.
    return f"sha256/{hash_[:2]}/{hash_[2:4]}/{hash_}/{filename}"


@dataclass
class Manifest:
    content_hash: str
    source_type: str
    source_url: str | None
    fetched_at: str
    ingestor_version: str
    ext: str                       # artifact file extension, so the blob can be reloaded
    annotates: str | None = None

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), indent=2, sort_keys=True).encode()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_artifact(
    store: BlobStore,
    data: bytes,
    *,
    source_type: str,
    ingestor_version: str,
    ext: str,
    source_url: str | None = None,
    annotates: str | None = None,
) -> tuple[str, Manifest]:
    """Hash → write blob + manifest.json → return (hash, manifest).

    Idempotent: identical bytes produce the same hash and overwrite in place.
    """
    h = content_hash(data)
    manifest = Manifest(
        content_hash=h,
        source_type=source_type,
        source_url=source_url,
        fetched_at=_now_iso(),
        ingestor_version=ingestor_version,
        ext=ext,
        annotates=annotates,
    )
    store.write(raw_key(h, f"artifact.{ext}"), data)
    store.write(raw_key(h, "manifest.json"), manifest.to_json())
    return h, manifest


def load_manifest(store: BlobStore, hash_: str) -> Manifest:
    data = json.loads(store.read(raw_key(hash_, "manifest.json")))
    return Manifest(**data)


def load_artifact(store: BlobStore, manifest: Manifest) -> bytes:
    return store.read(raw_key(manifest.content_hash, f"artifact.{manifest.ext}"))
