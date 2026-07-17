import pytest

from pipeline.config import Settings
from pipeline.db import bootstrap
from pipeline.llm.base import Completion
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
        },
    }
    s = Settings(raw=raw, config_path=tmp_path / "pipeline.yaml", root=tmp_path)
    VaultWriter(s.vault_dir).ensure_layout()
    s.prompts_dir.mkdir(parents=True, exist_ok=True)
    (s.prompts_dir / "extract_claims_v1.md").write_text("Extract claims. Return a JSON array.")
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

    Yields a mutable holder — set ``holder['text']`` before running a stage to
    control the response text.
    """
    from pipeline.llm import registry

    holder = {"text": '[{"claim": "Taste is the differentiator.", "quote": "Taste is the differentiator."}]'}

    class _Fake:
        name = "fake"
        supports_batch = False

        def complete(self, messages, model, params):
            return Completion(
                text=holder["text"],
                provider="fake",
                model=model,
                tokens_in=11,
                tokens_out=7,
                usd=0.0001,
                latency_ms=42,
            )

    monkeypatch.setattr(registry, "get_provider", lambda settings, name: _Fake())
    return holder
