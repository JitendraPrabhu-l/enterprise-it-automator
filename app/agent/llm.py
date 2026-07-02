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
"""

from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.config import get_settings


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
        api_key=settings.groq_api_key,
        temperature=0,
    )


def _build_anthropic() -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key,
        temperature=0,
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
        url=settings.watsonx_url,
        apikey=settings.watsonx_api_key,
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
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        temperature=0,
    )


_BUILDERS = {
    "groq": _build_groq,
    "anthropic": _build_anthropic,
    "watsonx": _build_watsonx,
    "openrouter": _build_openrouter,
}


@lru_cache
def get_llm() -> BaseChatModel:
    provider = get_settings().llm_provider.lower()
    try:
        builder = _BUILDERS[provider]
    except KeyError:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={provider!r}. Choose one of {list(_BUILDERS)}."
        ) from None
    return builder()
