"""OpenAI-compatible provider adapter.

One adapter, three providers: OpenAI-direct, OpenRouter, and Ollama all speak the
same `/v1/chat/completions` API — they differ only in `base_url`, key, and model
ids (all config, no code). Cost is computed from a per-model price map when one is
configured (OpenAI); providers without pricing (Ollama = local) report `usd=0`.
Latency is measured here so every call feeds the cost/latency ledger (plan §3).
"""
from __future__ import annotations

import time

from pipeline.llm.base import Completion, Embeddings, LLMError, Message


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        pricing: dict | None = None,
        supports_batch: bool = False,
        timeout: float = 60.0,
    ):
        from openai import OpenAI  # lazy: only imported when a provider is built

        self.name = name
        self.supports_batch = supports_batch
        # OpenAI SDK requires a non-empty key even for keyless endpoints (Ollama).
        self._client = OpenAI(base_url=base_url, api_key=api_key or "not-needed", timeout=timeout)
        self._pricing = pricing or {}

    def complete(self, messages: list[Message], model: str, params: dict) -> Completion:
        payload = [{"role": m.role, "content": m.content} for m in messages]
        t0 = time.monotonic()
        try:
            resp = self._client.chat.completions.create(model=model, messages=payload, **(params or {}))
        except Exception as e:  # network / auth / bad-request → typed, contextful failure
            raise LLMError(f"{self.name}/{model}: {e}") from e
        latency_ms = int((time.monotonic() - t0) * 1000)

        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        return Completion(
            text=text,
            provider=self.name,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usd=self._cost(model, tokens_in, tokens_out),
            latency_ms=latency_ms,
        )

    def embed(self, texts: list[str], model: str) -> Embeddings:
        t0 = time.monotonic()
        try:
            resp = self._client.embeddings.create(model=model, input=texts)
        except Exception as e:
            raise LLMError(f"{self.name}/{model} (embed): {e}") from e
        latency_ms = int((time.monotonic() - t0) * 1000)

        vectors = [list(d.embedding) for d in resp.data]
        usage = getattr(resp, "usage", None)
        tokens = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "total_tokens", 0) or 0)
        return Embeddings(
            vectors=vectors,
            provider=self.name,
            model=model,
            tokens=tokens,
            usd=self._cost(model, tokens, 0),  # embeddings priced on input only
            latency_ms=latency_ms,
        )

    def _cost(self, model: str, tokens_in: int, tokens_out: int) -> float:
        """USD from the configured price map (per 1M tokens); 0 when unpriced."""
        price = self._pricing.get(model)
        if not price:
            return 0.0
        return tokens_in / 1_000_000 * price["in"] + tokens_out / 1_000_000 * price.get("out", 0.0)
