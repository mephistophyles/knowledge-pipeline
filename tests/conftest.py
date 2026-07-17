import hashlib

import pytest

from pipeline.config import Settings
from pipeline.db import bootstrap
from pipeline.llm.base import Completion, Embeddings
from pipeline.vault import VaultWriter


@pytest.fixture
def settings(tmp_path):
    """A Settings pointed entirely at a tmp dir (local blob store).

    Includes a `fake` provider + an extract_claims stage config + a prompt file so
    the derivation chain resolves offline; `fake_claims` patches the actual call.
    """
    raw = {
        "pipeline_version": "0.1.0-test",
        "paths": {"db": "pipeline.db", "intermediate": "intermediate", "vault": "vault"},
        "storage": {"blobstore": "local", "local": {"root": "raw"}},
        "worker": {"poll_interval_seconds": 0.01, "max_attempts": 3},
        "providers": {
            "fake": {"type": "openai_compat", "base_url": "http://fake", "api_key_env": None},
        },
        "models": {
            "extract_claims": {"provider": "fake", "model": "fake-1", "params": {}, "prompt_version": "v1"},
            "dedup": {"provider": "fake", "model": "fake-confirm", "params": {}, "prompt_version": "v1"},
            "entities": {"provider": "fake", "model": "fake-ent", "params": {}, "prompt_version": "v1"},
        },
        "embeddings": {"provider": "fake", "model": "fake-embed"},
        "dedup": {"max_distance": 0.6, "shortlist_k": 5},
        "entities": {"max_distance": 0.6, "shortlist_k": 5},
    }
    s = Settings(raw=raw, config_path=tmp_path / "pipeline.yaml", root=tmp_path)
    VaultWriter(s.vault_dir).ensure_layout()
    s.prompts_dir.mkdir(parents=True, exist_ok=True)
    (s.prompts_dir / "extract_claims_v1.md").write_text("Extract claims. Return a JSON array.")
    (s.prompts_dir / "dedup_confirm_v1.md").write_text("Same claim? Return {\"same\": bool}.")
    (s.prompts_dir / "entities_extract_v1.md").write_text("Extract entities. Return JSON {name,type}.")
    (s.prompts_dir / "entities_confirm_v1.md").write_text("Same entity? Return {\"same\": bool}.")
    return s


@pytest.fixture
def conn(settings):
    return bootstrap(settings.db_path)


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    """Providers cache by id(settings); clear between tests so a reused object id
    can't hand back a stale adapter."""
    from pipeline.llm import registry

    registry._CACHE.clear()
    yield
    registry._CACHE.clear()


@pytest.fixture
def fake_claims(monkeypatch):
    """Patch the provider registry so LLM stages run offline with canned output.

    Yields a mutable holder controlling the fake:
      - ``text``   → extract_claims response (a JSON claims array)
      - ``same``   → dedup-confirm verdict (True → attest, False → distinct)
      - ``vector`` → fixed embedding for every text (None → per-text hash vector)
    The fake distinguishes an extract call from a confirm call by the prompt.
    """
    from pipeline.llm import registry

    holder = {
        "text": '[{"claim": "Taste is the differentiator.", "quote": "Taste is the differentiator."}]',
        "entities": '[{"name": "OpenAI", "type": "company"}]',
        "same": False,
        "vector": None,
    }

    class _Fake:
        name = "fake"
        supports_batch = False

        def complete(self, messages, model, params):
            system = messages[0].content.lower()
            if "same" in system:  # a confirm/tiebreak call (dedup or entities)
                text = '{"same": true}' if holder["same"] else '{"same": false}'
            elif "entit" in system:  # entity extraction
                text = holder["entities"]
            else:  # claim extraction
                text = holder["text"]
            return Completion(text=text, provider="fake", model=model, tokens_in=11, tokens_out=7, usd=0.0001, latency_ms=42)

        def embed(self, texts, model):
            vectors = []
            for t in texts:
                if holder["vector"] is not None:
                    vectors.append(list(holder["vector"]))
                else:  # deterministic 8-dim vector from the text
                    digest = hashlib.sha256(t.encode()).digest()[:8]
                    vectors.append([b / 255.0 for b in digest])
            return Embeddings(vectors=vectors, provider="fake", model=model, tokens=len(texts), usd=0.0, latency_ms=1)

    monkeypatch.setattr(registry, "get_provider", lambda settings, name: _Fake())
    return holder
