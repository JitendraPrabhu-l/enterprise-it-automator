"""Tests for app/agent/llm.py's ainvoke_with_fallback/FallbackLLM — provider-
outage failover, distinct from the node-level RetryPolicy's same-provider
retry-with-backoff (app/agent/graph.py). See the module docstring in
app/agent/llm.py for the full design rationale.
"""

import pytest

from app.agent.llm import FallbackLLM, ainvoke_with_fallback, get_llm_for_provider
from app.config import get_settings
from app.mcp_server.circuit_breaker import reset_all_breakers


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """Always succeeds with a fixed reply."""

    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _FakeResponse(self.reply)


class _FailingLLM:
    """Always raises the given exception."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise self.exc


@pytest.fixture(autouse=True)
def _clear_state():
    get_settings.cache_clear()
    get_llm_for_provider.cache_clear()
    reset_all_breakers()
    yield
    get_settings.cache_clear()
    get_llm_for_provider.cache_clear()
    reset_all_breakers()


def _configure(monkeypatch, *, primary, groq_key="", anthropic_key="", openrouter_key=""):
    monkeypatch.setenv("LLM_PROVIDER", primary)
    monkeypatch.setenv("GROQ_API_KEY", groq_key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", anthropic_key)
    monkeypatch.setenv("OPENROUTER_API_KEY", openrouter_key)
    monkeypatch.delenv("WATSONX_API_KEY", raising=False)
    monkeypatch.delenv("WATSONX_PROJECT_ID", raising=False)
    get_settings.cache_clear()


async def test_primary_success_never_touches_fallback(monkeypatch):
    _configure(monkeypatch, primary="groq", groq_key="k", openrouter_key="k2")
    primary_llm = _FakeLLM("primary reply")
    monkeypatch.setattr("app.agent.llm.get_llm_for_provider", lambda p: primary_llm)

    response, model_name = await ainvoke_with_fallback([])

    assert response.content == "primary reply"
    assert primary_llm.calls == 1
    assert model_name == get_settings().groq_model


async def test_falls_over_to_next_configured_provider_on_transient_failure(monkeypatch):
    _configure(monkeypatch, primary="groq", groq_key="k", openrouter_key="k2")

    fake_llms = {
        "groq": _FailingLLM(ConnectionError("groq down")),
        "openrouter": _FakeLLM("fallback reply"),
    }
    monkeypatch.setattr("app.agent.llm.get_llm_for_provider", lambda p: fake_llms[p])

    response, model_name = await ainvoke_with_fallback([])

    assert response.content == "fallback reply"
    assert fake_llms["groq"].calls == 1
    assert fake_llms["openrouter"].calls == 1
    assert model_name == get_settings().openrouter_model


async def test_skips_providers_without_credentials(monkeypatch):
    """anthropic has no key set — it must never be tried even though it's
    earlier/later in preference than a configured one, and openrouter (which
    DOES have a key) must be the one that actually gets called."""
    _configure(monkeypatch, primary="groq", groq_key="k", openrouter_key="k2")

    fake_llms = {
        "groq": _FailingLLM(ConnectionError("groq down")),
        "openrouter": _FakeLLM("fallback reply"),
    }

    def _get(provider):
        if provider == "anthropic":
            raise AssertionError("anthropic has no credentials and must never be constructed")
        return fake_llms[provider]

    monkeypatch.setattr("app.agent.llm.get_llm_for_provider", _get)

    response, _ = await ainvoke_with_fallback([])
    assert response.content == "fallback reply"


async def test_raises_primary_exception_when_no_fallback_configured(monkeypatch):
    _configure(monkeypatch, primary="groq", groq_key="k")  # no other provider has a key
    primary_exc = ConnectionError("groq down")
    monkeypatch.setattr("app.agent.llm.get_llm_for_provider", lambda p: _FailingLLM(primary_exc))

    with pytest.raises(ConnectionError, match="groq down"):
        await ainvoke_with_fallback([])


async def test_raises_primary_exception_when_every_candidate_fails(monkeypatch):
    _configure(monkeypatch, primary="groq", groq_key="k", openrouter_key="k2")
    primary_exc = ConnectionError("groq down")

    fake_llms = {
        "groq": _FailingLLM(primary_exc),
        "openrouter": _FailingLLM(ConnectionError("openrouter also down")),
    }
    monkeypatch.setattr("app.agent.llm.get_llm_for_provider", lambda p: fake_llms[p])

    with pytest.raises(ConnectionError, match="groq down"):
        await ainvoke_with_fallback([])


async def test_non_transient_error_does_not_try_fallback(monkeypatch):
    """A ValueError/TypeError/KeyError-shaped failure (matching
    is_transient_error's classification) will fail identically against
    every provider — no point burning the fallback chain on it."""
    _configure(monkeypatch, primary="groq", groq_key="k", openrouter_key="k2")

    fallback_llm = _FakeLLM("should never be called")
    fake_llms = {"groq": _FailingLLM(ValueError("bad request shape")), "openrouter": fallback_llm}
    monkeypatch.setattr("app.agent.llm.get_llm_for_provider", lambda p: fake_llms[p])

    with pytest.raises(ValueError, match="bad request shape"):
        await ainvoke_with_fallback([])
    assert fallback_llm.calls == 0


async def test_open_circuit_breaker_skips_straight_to_fallback(monkeypatch):
    _configure(monkeypatch, primary="groq", groq_key="k", openrouter_key="k2")

    from app.mcp_server.circuit_breaker import get_breaker

    breaker = get_breaker("llm:groq")
    for _ in range(breaker.failure_threshold):
        breaker.record_failure()
    assert not breaker.allow_request()

    groq_llm = _FakeLLM("should not be called — breaker is open")
    openrouter_llm = _FakeLLM("fallback reply")
    monkeypatch.setattr(
        "app.agent.llm.get_llm_for_provider",
        lambda p: {"groq": groq_llm, "openrouter": openrouter_llm}[p],
    )

    response, _ = await ainvoke_with_fallback([])
    assert response.content == "fallback reply"
    assert groq_llm.calls == 0


async def test_fallback_llm_ainvoke_updates_last_model_name(monkeypatch):
    _configure(monkeypatch, primary="groq", groq_key="k", openrouter_key="k2")

    fake_llms = {
        "groq": _FailingLLM(ConnectionError("groq down")),
        "openrouter": _FakeLLM("fallback reply"),
    }
    monkeypatch.setattr("app.agent.llm.get_llm_for_provider", lambda p: fake_llms[p])

    fallback = FallbackLLM()
    assert fallback.last_model_name == get_settings().groq_model  # primary, before any call

    response = await fallback.ainvoke([])
    assert response.content == "fallback reply"
    assert fallback.last_model_name == get_settings().openrouter_model
