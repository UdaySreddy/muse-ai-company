"""AI Team OS — LLM factory for standalone execution (no Claude Code).

Historically the orchestrator nodes instantiated ``ChatAnthropic`` directly.
That is a *direct API* client — it never needed Claude Code — but the model
was not injectable, so there was no supported way to run the team with a
different provider, or offline in tests/demos without an API key.

This factory is the single place that builds the chat model used by the
orchestrator nodes:

- ``anthropic`` (default): direct Anthropic API via ``langchain-anthropic``.
  Requires ``ANTHROPIC_API_KEY``. No Claude Code installation is involved.
- ``openai``: direct OpenAI API via ``langchain-openai`` (optional package).
  Requires ``OPENAI_API_KEY``.
- ``fake``: offline deterministic model (``FakeListChatModel``) that cycles
  through ``responses``. No network, no key, no Claude Code — used for
  smoke tests, demos, and CI.

Selection order: explicit argument > ``configurable`` entry (handled by
:func:`resolve_llm`) > environment (``AITEAM_LLM_PROVIDER`` /
``AITEAM_LLM_MODEL``) > defaults.

All provider imports are lazy: importing this module (or the orchestrator)
must never require a provider package that is not installed.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = "gpt-4o"
PROVIDER_ENV = "AITEAM_LLM_PROVIDER"
MODEL_ENV = "AITEAM_LLM_MODEL"

_LANGGRAPH_HINT = "pip install 'ai-team-os[langgraph]'"


def build_llm(
    provider: str | None = None,
    model: str | None = None,
    *,
    responses: list[str] | None = None,
) -> Any:
    """Build a chat model for the given provider.

    Args:
        provider: ``anthropic`` | ``openai`` | ``fake``. Defaults to the
            ``AITEAM_LLM_PROVIDER`` env var, then ``anthropic``.
        model: Provider model name. Defaults to ``AITEAM_LLM_MODEL`` env var,
            then the provider default.
        responses: Only used by the ``fake`` provider — canned replies,
            cycled in order.

    Returns:
        A LangChain chat model with an async ``ainvoke`` interface.

    Raises:
        ValueError: Unknown provider.
        RuntimeError: Provider package missing or its API key is not set.
    """
    name = (provider or os.environ.get(PROVIDER_ENV) or "anthropic").strip().lower()
    model = model or os.environ.get(MODEL_ENV) or None

    if name == "fake":
        try:
            from langchain_core.language_models.fake_chat_models import (
                FakeListChatModel,
            )
        except ImportError as e:
            raise RuntimeError(
                f"The fake LLM provider needs langchain-core. Install: {_LANGGRAPH_HINT}"
            ) from e
        return FakeListChatModel(responses=list(responses) if responses else ["(fake llm response)"])

    if name == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:
            raise RuntimeError(
                f"The anthropic provider needs langchain-anthropic. Install: {_LANGGRAPH_HINT}"
            ) from e
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Standalone mode calls the Anthropic "
                "API directly (no Claude Code needed) — export your API key, or "
                "use --provider fake for an offline smoke run."
            )
        return ChatAnthropic(model=model or DEFAULT_MODEL)

    if name == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise RuntimeError(
                "The openai provider needs the optional langchain-openai package. "
                "Install: pip install langchain-openai"
            ) from e
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set — export your API key, or use "
                "--provider fake for an offline smoke run."
            )
        return ChatOpenAI(model=model or DEFAULT_OPENAI_MODEL)

    raise ValueError(
        f"Unknown LLM provider: {name!r} (expected 'anthropic', 'openai' or 'fake')"
    )


def resolve_llm(configurable: dict | None, model: str | None = None) -> Any:
    """Resolve the chat model for an orchestrator node.

    An explicitly injected model (``configurable["llm"]``) always wins — this
    is what tests and embedded hosts use. Otherwise the model is built from
    ``configurable["llm_provider"]`` / environment / defaults, preserving the
    pre-factory behaviour (direct Anthropic API) when nothing is configured.
    """
    configurable = configurable or {}
    llm = configurable.get("llm")
    if llm is not None:
        return llm
    return build_llm(provider=configurable.get("llm_provider"), model=model)
