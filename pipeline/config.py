"""Config loading + resolved runtime settings.

`Settings` is the single object every layer takes: it knows where the DB, raw
store, intermediates and vault live, and which blob store backs the raw store.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "config/pipeline.yaml"


def _project_root() -> Path:
    # The repo root is the parent of the `pipeline/` package.
    return Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    raw: dict[str, Any]
    config_path: Path
    root: Path

    @staticmethod
    def load(config_path: str | os.PathLike | None = None) -> "Settings":
        root = _project_root()
        path = Path(config_path or os.environ.get("PIPELINE_CONFIG", DEFAULT_CONFIG_PATH))
        if not path.is_absolute():
            path = root / path
        raw = yaml.safe_load(path.read_text())
        return Settings(raw=raw, config_path=path, root=root)

    def _path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else self.root / p

    @property
    def pipeline_version(self) -> str:
        return str(self.raw.get("pipeline_version", "0.0.0"))

    @property
    def db_path(self) -> Path:
        return self._path("db")

    @property
    def intermediate_dir(self) -> Path:
        return self._path("intermediate")

    @property
    def vault_dir(self) -> Path:
        return self._path("vault")

    @property
    def poll_interval(self) -> float:
        return float(self.raw.get("worker", {}).get("poll_interval_seconds", 2.0))

    @property
    def max_attempts(self) -> int:
        return int(self.raw.get("worker", {}).get("max_attempts", 3))

    @cached_property
    def blobstore(self):
        from pipeline.storage.blobstore import get_blobstore

        return get_blobstore(self)
