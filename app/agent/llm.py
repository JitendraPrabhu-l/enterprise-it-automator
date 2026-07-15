"""Pluggable LLM adapter.

Swapping backends is a one-line config change
(LLM_PROVIDER=groq|anthropic|watsonx|openrouter) rather than a code change, so
the same agent graph can be demoed on a free Groq key today and pointed at
watsonx/Granite, Claude, or an OpenRouter free-tier model later without
touching agent/graph.py.

OpenRouter exists as a credential-free fallback: IBM's watsonx.ai Lite plan
requires a credit card on file to provision a project even though usage
itself is free, which blocks watsonx access until that's set up. OpenRouter's
free-tier models need only an API key (no card) and are exposed through an
OpenAI-compatible API, so ChatOpenAI can talk to them by pointing base_url at
OpenRouter instead of OpenAI.

ainvoke_with_fallback() (below) is the actual call path every agent node
uses — not raw get_llm().ainvoke() — so that a sustained outage on the
configured primary provider automatically fails over to whichever OTHER
configured providers have credentials set, instead of failing every ticket
until someone notices and flips LLM_PROVIDER by hand. This is a SEPARATE
concern from the node-level RetryPolicy in app/agent/graph.py: retry-with-
backoff already covers a transient blip against the SAME provider; fallback
covers "that provider is down for longer than 4 retries can absorb, and a
different one is available right now."
"""

import logging
from functools import lru_cache

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from app import metrics
from app.config import get_settings
from app.mcp_server.circuit_breaker import CircuitOpenError, get_breaker

logger = logging.getLogger(__name__)


def _build_groq() -> BaseChatModel:
    from langchain_groq import ChatGroq

    settings = get_settings()
    if not settings.groq_api_key:
        raise RuntimeError(
            "LLM_PROVIDER=groq but GROQ_API_KEY is not set. "
            "Get a free key at https://console.groq.com/keys"
        )
    return ChatGroq(
        model=settings.groq_model,
        api_key=SecretStr(settings.groq_api_key),
        temperature=0,
    )


def _build_anthropic() -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
    # model_name= is the field's real name (model= is its runtime alias,
    # which mypy can't see); timeout/stop are required by the type stubs
    # despite having working runtime defaults.
    return ChatAnthropic(
        model_name=settings.anthropic_model,
        api_key=SecretStr(settings.anthropic_api_key),
        temperature=0,
        timeout=None,
        stop=None,
    )


def _build_watsonx() -> BaseChatModel:
    from langchain_ibm import ChatWatsonx

    settings = get_settings()
    if not settings.watsonx_api_key or not settings.watsonx_project_id:
        raise RuntimeError(
            "LLM_PROVIDER=watsonx but WATSONX_API_KEY / WATSONX_PROJECT_ID are not set."
        )
    return ChatWatsonx(
        model_id=settings.watsonx_model,
        url=SecretStr(settings.watsonx_url),
        apikey=SecretStr(settings.watsonx_api_key),
        project_id=settings.watsonx_project_id,
        params={"temperature": 0},
    )


def _build_openrouter() -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError(
            "LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set. "
            "Get a free key at https://openrouter.ai/keys"
        )
    return ChatOpenAI(
        model=settings.openrouter_model,
        api_key=SecretStr(settings.openrouter_api_key),
        base_url=settings.openrouter_base_url,
        temperature=0,
    )


_BUILDERS = {
    "groq": _build_groq,
    "anthropic": _build_anthropic,
    "watsonx": _build_watsonx,
    "openrouter": _build_openrouter,
}

# Fixed fallback preference order — free/fast providers first, since a
# fallback firing means the configured primary is already unavailable and
# this is now about getting SOME response rather than the ideal one. Not
# configurable via Settings: with only 4 providers, a fixed order is easy
# to reason about, and making it configurable would need its own validation
# (must contain the primary, must only reference known providers) for a
# feature that's already an edge case.
_FALLBACK_ORDER = ["groq", "openrouter", "anthropic", "watsonx"]


def _model_name_for(provider: str) -> str:
    """Model identifier for ANY provider (not just the configured primary)
    — used by ainvoke_with_fallback below to report which model actually
    served a call, since that can differ from settings.llm_provider once a
    fallback has fired. app/agent/graph.py's _llm_model_name() is the
    primary-only version of this same lookup, kept separate because most
    call sites only ever need the primary and importing this module there
    would be a one-function dependency for no benefit.
    """
    settings = get_settings()
    return {
        "groq": settings.groq_model,
        "anthropic": settings.anthropic_model,
        "watsonx": settings.watsonx_model,
        "openrouter": settings.openrouter_model,
    }.get(provider, provider)


def _has_credentials(provider: str) -> bool:
    settings = get_settings()
    return bool(
        {
            "groq": settings.groq_api_key,
            "anthropic": settings.anthropic_api_key,
            "watsonx": settings.watsonx_api_key and settings.watsonx_project_id,
            "openrouter": settings.openrouter_api_key,
        }.get(provider)
    )


@lru_cache
def get_llm_for_provider(provider: str) -> BaseChatModel:
    try:
        builder = _BUILDERS[provider]
    except KeyError:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={provider!r}. Choose one of {list(_BUILDERS)}."
        ) from None
    return builder()


def get_llm() -> BaseChatModel:
    return get_llm_for_provider(get_settings().llm_provider.lower())


async def ainvoke_with_fallback(messages) -> tuple[object, str]:
    """The actual call path every agent node uses in place of raw
    get_llm().ainvoke() — see the module docstring for why this exists
    (provider-outage failover, distinct from the node-level RetryPolicy's
    same-provider retry-with-backoff).

    Tries the configured primary provider first, gated by its own circuit
    breaker (namespaced "llm:<provider>", reusing app/mcp_server's generic
    CircuitBreaker — same three-state CLOSED/OPEN/HALF_OPEN machinery
    already proven for MCP domain isolation, just keyed differently). On a
    transient failure — or if that provider's breaker is already open from
    recent failures — tries each OTHER provider in _FALLBACK_ORDER that has
    credentials configured, in order, until one succeeds.

    Returns (response, model_name_that_served_it) — callers must use the
    returned model name for metrics/tracing (record_llm_call), not
    _llm_model_name()/settings.llm_provider, since those only name the
    PRIMARY and would misattribute a fallback response to the wrong model.

    Raises the primary provider's own exception (not a fallback's) if every
    candidate fails or none besides the primary have credentials — the
    primary's error is almost always the more actionable one to surface
    (e.g. "GROQ_API_KEY not set" beats a fallback's generic connection
    error), and it's the one an operator configured this deployment to use.
    """
    from app.agent.mcp_client import is_transient_error

    primary_provider = get_settings().llm_provider.lower()
    candidates = [primary_provider] + [
        p for p in _FALLBACK_ORDER if p != primary_provider and _has_credentials(p)
    ]

    primary_exc: Exception | None = None
    for i, provider in enumerate(candidates):
        breaker = get_breaker(f"llm:{provider}")
        if not breaker.allow_request():
            if i == 0:
                primary_exc = CircuitOpenError(
                    f"Circuit breaker open for LLM provider {provider!r} — "
                    "too many recent failures, refusing until the recovery timeout elapses."
                )
            continue

        try:
            llm = get_llm_for_provider(provider)
            response = await llm.ainvoke(messages)
        except Exception as exc:
            breaker.record_failure()
            if i == 0:
                primary_exc = exc
            if not is_transient_error(exc):
                # A non-transient error (bad request shape, auth failure)
                # will fail identically against every other provider too —
                # no point burning the fallback chain retrying it.
                break
            logger.warning(
                "LLM provider %r failed (%s), %s", provider, exc,
                "trying next fallback" if i + 1 < len(candidates) else "no more fallbacks configured",
            )
            continue

        breaker.record_success()
        if i > 0:
            logger.warning("LLM fallback served this call: primary=%r, served_by=%r", primary_provider, provider)
            metrics.LLM_FALLBACK_TOTAL.labels(primary=primary_provider, served_by=provider).inc()
        return response, _model_name_for(provider)

    assert primary_exc is not None
    raise primary_exc


class FallbackLLM:
    """Thin adapter exposing the same .ainvoke(messages) interface every
    agent-graph node already calls on a raw BaseChatModel (and that tests'
    _FakeLLM fixtures already implement), so plan_node/classify_node/etc.
    can pass THIS in wherever they previously passed get_llm() with zero
    other code changes — the fallback behavior in ainvoke_with_fallback
    above is entirely internal to this one method.

    last_model_name is set after each call to whichever provider actually
    served it (not necessarily the configured primary) — callers use it for
    record_llm_call's model= argument instead of _llm_model_name(), which
    only ever names the primary.
    """

    def __init__(self) -> None:
        self.last_model_name: str = _model_name_for(get_settings().llm_provider.lower())

    async def ainvoke(self, messages):
        response, model_name = await ainvoke_with_fallback(messages)
        self.last_model_name = model_name
        return response
