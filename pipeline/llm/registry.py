"""Build providers from `config/pipeline.yaml` and hand them to stages.

Providers are declared once under `providers:`; each stage's `models:` entry names
one by key. Adapters are cached per (config, name) so we don't rebuild an HTTP
client on every call. Handlers reach providers only through `get_provider` — tests
monkeypatch this one function to inject a fake, no network required.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pipeline.llm.base import LLMError, Provider
from pipeline.llm.openai_compat import OpenAICompatProvider

if TYPE_CHECKING:
    from pipeline.config import Settings

_CACHE: dict[tuple[int, str], Provider] = {}


def build_provider(name: str, cfg: dict) -> Provider:
    ptype = cfg.get("type", "openai_compat")
    if ptype != "openai_compat":
        raise ValueError(f"provider {name!r}: unknown type {ptype!r}")

    api_key = None
    env = cfg.get("api_key_env")
    if env:  # keyless providers (Ollama) leave api_key_env null
        api_key = os.environ.get(env)
        if not api_key:
            raise LLMError(f"provider {name!r}: ${env} is not set (needed to reach {cfg.get('base_url')})")
    return OpenAICompatProvider(
        name=name,
        base_url=cfg["base_url"],
        api_key=api_key,
        pricing=cfg.get("pricing"),
        supports_batch=bool(cfg.get("supports_batch", False)),
        timeout=float(cfg.get("timeout", 60.0)),
    )


def get_provider(settings: "Settings", name: str) -> Provider:
    cache_key = (id(settings), name)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    providers = settings.providers_config
    if name not in providers:
        raise LLMError(f"no provider {name!r} configured under `providers:`")
    provider = build_provider(name, providers[name])
    _CACHE[cache_key] = provider
    return provider
