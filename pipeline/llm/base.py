"""Provider-agnostic LLM interface (the system's core swap point, plan §6.5).

A `Provider` turns messages + a model id + params into a `Completion` and reports
what it cost and how long it took. Concrete providers (OpenAI-compatible, and
later Bedrock / native-batch adapters) live beside this file; nothing upstream
knows which one ran — the per-stage config picks it.

`gen_key` is the *generating key*: the full tuple `(provider, model, params,
prompt_version, input_hash)` that reproduces a derivation (plan invariant 3).
It is stamped into every derived note's frontmatter and used to key intermediates
so two configs over the same source never clobber each other (eval-compare).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """Any provider-side failure, wrapped with provider/model context."""


@dataclass(frozen=True)
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    usd: float
    latency_ms: int
    raw: dict[str, Any] | None = None


@dataclass
class Embeddings:
    vectors: list[list[float]]
    provider: str
    model: str
    tokens: int
    usd: float
    latency_ms: int


@runtime_checkable
class Provider(Protocol):
    """What every provider adapter must offer. Kept deliberately small.

    `embed` is optional in spirit — only stages that need vectors call it, and a
    provider that can't embed simply raises. The OpenAI-compatible adapter serves
    both completions and embeddings over the same endpoint family.
    """

    name: str
    supports_batch: bool  # capability flag; batch *execution* is a later PR

    def complete(self, messages: list[Message], model: str, params: dict) -> Completion: ...

    def embed(self, texts: list[str], model: str) -> Embeddings: ...


def gen_key(
    *,
    provider: str,
    model: str,
    params: dict,
    prompt_version: str,
    input_hash: str,
) -> dict:
    """The reproducibility tuple for one derivation. Order-independent params."""
    return {
        "provider": provider,
        "model": model,
        "params": dict(params or {}),
        "prompt_version": prompt_version,
        "input_hash": input_hash,
    }


def gen_key_hash(key: dict) -> str:
    """Short stable digest of a gen_key — used in keyed intermediate filenames."""
    blob = json.dumps(key, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
