"""StageContext — everything a handler needs, plus intermediate persistence.

Intermediates live at ``data/intermediate/<hash>/<stage>.json`` and are written
*before* the job advances (plan invariant 6): a paused pipeline is inspectable
at exactly the point it stopped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import Settings
from pipeline.storage.blobstore import BlobStore
from pipeline.storage.manifest import Manifest, load_manifest
from pipeline.vault import VaultWriter


@dataclass
class StageContext:
    settings: Settings
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

    @staticmethod
    def build(settings: Settings, artifact_hash: str, stage: str, input_path: str | None) -> "StageContext":
        store = settings.blobstore
        return StageContext(
            settings=settings,
            artifact_hash=artifact_hash,
            stage=stage,
            manifest=load_manifest(store, artifact_hash),
            blobstore=store,
            vault=VaultWriter(settings.vault_dir),
            input_path=input_path,
        )
