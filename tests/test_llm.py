"""Unit coverage for the provider-agnostic LLM layer: cost math, the generating
key, lenient claim parsing, and registry resolution."""
import pytest

from pipeline.llm.base import gen_key, gen_key_hash
from pipeline.llm.openai_compat import OpenAICompatProvider
from pipeline.orchestrator.handlers import _parse_claims


# ── cost ledger math ──────────────────────────────────────────────────────────
def test_cost_from_price_map():
    p = OpenAICompatProvider(
        name="openai", base_url="http://x", api_key="k",
        pricing={"gpt-4o-mini": {"in": 0.15, "out": 0.60}},
    )
    assert p._cost("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)
    assert p._cost("gpt-4o-mini", 0, 0) == 0.0


def test_cost_zero_when_unpriced():
    # Ollama / local: no pricing → cost recorded as 0.
    p = OpenAICompatProvider(name="ollama", base_url="http://x", api_key=None)
    assert p._cost("gemma4:latest", 5000, 5000) == 0.0


# ── generating key ────────────────────────────────────────────────────────────
def test_gen_key_hash_distinguishes_params():
    base = dict(provider="openai", model="gpt-4o-mini", prompt_version="v1", input_hash="abc")
    a = gen_key(params={"temperature": 0}, **base)
    b = gen_key(params={"temperature": 1}, **base)
    # Different config → different key → keyed intermediates don't clobber (eval-compare).
    assert gen_key_hash(a) != gen_key_hash(b)


def test_gen_key_hash_is_order_independent():
    a = gen_key(provider="p", model="m", params={"a": 1, "b": 2}, prompt_version="v1", input_hash="h")
    b = gen_key(provider="p", model="m", params={"b": 2, "a": 1}, prompt_version="v1", input_hash="h")
    assert gen_key_hash(a) == gen_key_hash(b)


# ── lenient claim parsing ─────────────────────────────────────────────────────
def test_parse_plain_json_array():
    out = _parse_claims('[{"claim": "X is true", "quote": "X"}]')
    assert out == [{"text": "X is true", "quote": "X"}]


def test_parse_fenced_and_prose_wrapped():
    fenced = 'Here you go:\n```json\n[{"claim": "Y", "quote": "y"}]\n```'
    assert _parse_claims(fenced) == [{"text": "Y", "quote": "y"}]


def test_parse_object_with_claims_key():
    assert _parse_claims('{"claims": [{"claim": "Z"}]}') == [{"text": "Z", "quote": ""}]


def test_parse_empty_and_garbage():
    assert _parse_claims("[]") == []
    assert _parse_claims("not json at all") == []


# ── registry ──────────────────────────────────────────────────────────────────
def test_registry_missing_key_raises(settings, monkeypatch):
    from pipeline.llm import LLMError, registry

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings.raw["providers"]["openai"] = {
        "type": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    }
    with pytest.raises(LLMError):
        registry.get_provider(settings, "openai")


def test_registry_unknown_provider_raises(settings):
    from pipeline.llm import LLMError, registry

    with pytest.raises(LLMError):
        registry.get_provider(settings, "nope")


def test_empty_choices_raises_legible_error_not_typeerror():
    """A 200 response carrying an `error` payload and no choices — how OpenRouter
    reports a throttled free pool — must name the cause, not raise TypeError."""
    from pipeline.llm.base import LLMError, Message

    class _Resp:
        choices = None
        error = {"message": "rate limit exceeded: free-tier pool"}
        usage = None

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**_):
                    return _Resp()

    p = OpenAICompatProvider(name="fake", base_url="http://x", api_key=None)
    p._client = _Client()
    with pytest.raises(LLMError) as e:
        p.complete([Message("system", "s")], "m", {})
    assert "rate limit exceeded" in str(e.value)


# ── claim-parse salvage: every case below is a REAL response that silently
# produced zero claims on the 178-edition pilot (4.5% of editions) ────────────
def test_parse_survives_prompt_echoed_after_a_valid_array():
    """Most common failure: the model appends commentary that itself contains
    brackets, so first-'[' to last-']' swallows the prose and parses as nothing."""
    raw = ('[{"claim": "A real claim.", "quote": "a quote"}]\n'
           'Return [] only if there is genuinely no transferable insight.')
    assert _parse_claims(raw) == [{"text": "A real claim.", "quote": "a quote"}]


def test_parse_accepts_python_style_single_quotes():
    raw = "[{'claim': 'Single quoted.', 'quote': 'q'}]"
    assert _parse_claims(raw) == [{"text": "Single quoted.", "quote": "q"}]


def test_parse_salvages_siblings_when_one_entry_is_malformed():
    """One bad entry must not cost the whole edition."""
    raw = ('[{"claim": "Good one.", "quote": "q1"}, '
           '{"claim": "Bad one.", "quote": \\"broken escape\\"}, '
           '{"claim": "Good two.", "quote": "q2"}]')
    got = [c["text"] for c in _parse_claims(raw)]
    assert "Good one." in got and "Good two." in got


def test_parse_salvages_a_response_truncated_at_the_token_cap():
    raw = '[{"claim": "Complete.", "quote": "q"}, {"claim": "Cut off mid-ob'
    assert _parse_claims(raw) == [{"text": "Complete.", "quote": "q"}]


def test_parse_still_returns_empty_for_a_genuine_empty_array():
    """Recovery must not invent claims where the model correctly found none."""
    assert _parse_claims("[]") == []


def test_parse_still_reads_a_wrapper_object():
    assert _parse_claims('{"claims": [{"claim": "Wrapped.", "quote": "q"}]}') == [
        {"text": "Wrapped.", "quote": "q"}
    ]
