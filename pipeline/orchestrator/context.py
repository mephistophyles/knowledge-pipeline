"""StageContext — everything a handler needs, plus intermediate persistence.

Intermediates live at ``data/intermediate/<hash>/<stage>.json`` and are written
*before* the job advances (plan invariant 6): a paused pipeline is inspectable
at exactly the point it stopped.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import Settings
from pipeline.llm.base import gen_key_hash
from pipeline.storage.blobstore import BlobStore
from pipeline.storage.manifest import Manifest, load_manifest
from pipeline.vault import VaultWriter


@dataclass
class StageContext:
    settings: Settings
    conn: sqlite3.Connection
    artifact_hash: str
    stage: str
    manifest: Manifest
    blobstore: BlobStore
    vault: VaultWriter
    input_path: str | None

    @property
    def intermediate_dir(self) -> Path:
        return self.settings.intermediate_dir / self.artifact_hash

    def write_intermediate(self, filename: str, payload: dict) -> str:
        d = self.intermediate_dir
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return str(path)

    def write_intermediate_keyed(self, stage: str, gen_key: dict, payload: dict) -> str:
        """Persist a producer output keyed by its generating key, so two configs
        over the same source (eval-compare) never clobber each other."""
        return self.write_intermediate(f"{stage}.{gen_key_hash(gen_key)}.json", payload)

    @staticmethod
    def build(
        settings: Settings,
        conn: sqlite3.Connection,
        artifact_hash: str,
        stage: str,
        input_path: str | None,
    ) -> "StageContext":
        store = settings.blobstore
        return StageContext(
            settings=settings,
            conn=conn,
            artifact_hash=artifact_hash,
            stage=stage,
            manifest=load_manifest(store, artifact_hash),
            blobstore=store,
            vault=VaultWriter(settings.vault_dir),
            input_path=input_path,
        )
