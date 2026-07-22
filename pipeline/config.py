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


@dataclass(frozen=True)
class StageModel:
    """Resolved per-stage LLM config: which provider, model, params, prompt."""

    provider: str
    model: str
    params: dict[str, Any]
    prompt_version: str
    batch: bool


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
    def prompts_dir(self) -> Path:
        # Prompts live beside the config file, at config/prompts/.
        return self.config_path.parent / "prompts"

    @property
    def providers_config(self) -> dict[str, Any]:
        return self.raw.get("providers", {}) or {}

    @property
    def embeddings_config(self) -> dict[str, Any]:
        return self.raw.get("embeddings", {}) or {}

    @property
    def email_config(self) -> dict[str, Any]:
        return self.raw.get("email", {}) or {}

    @property
    def max_parallel(self) -> int:
        """Max concurrent dashboard-triggered execution tasks (default 1 — local
        Ollama tasks must not stampede)."""
        return int((self.raw.get("execution", {}) or {}).get("max_parallel", 1))

    @property
    def dedup_config(self) -> dict[str, Any]:
        return self._resolution_config("dedup")

    @property
    def entities_config(self) -> dict[str, Any]:
        return self._resolution_config("entities")

    def _resolution_config(self, key: str) -> dict[str, Any]:
        cfg = self.raw.get(key, {}) or {}
        return {"max_distance": float(cfg.get("max_distance", 0.6)), "shortlist_k": int(cfg.get("shortlist_k", 5))}

    def stage_model(self, stage: str) -> "StageModel":
        """Resolve the {provider, model, params, prompt_version} for one stage."""
        m = (self.raw.get("models", {}) or {}).get(stage)
        if not m:
            raise KeyError(f"no `models:` entry for stage {stage!r} in {self.config_path}")
        return StageModel(
            provider=m["provider"],
            model=m["model"],
            params=dict(m.get("params") or {}),
            prompt_version=str(m.get("prompt_version", "v1")),
            batch=bool(m.get("batch", False)),
        )

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
