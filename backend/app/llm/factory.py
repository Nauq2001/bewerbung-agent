"""Turn an ``agent_configs`` row into a provider instance.

The one place that maps a provider name to a class. Everywhere else branches on
``provider.supports(cap)``, never on the name.
"""

from __future__ import annotations

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.openai_provider import OpenAIProvider

_PROVIDERS = {
    "anthropic": AnthropicProvider,
    # Also covers any OpenAI-compatible endpoint via base_url: Ollama,
    # Together, Groq, vLLM. No separate adapter needed for those.
    "openai": OpenAIProvider,
}


def get_provider(provider: str, model: str, base_url: str | None = None) -> LLMProvider:
    cls = _PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(
            f"unknown provider {provider!r}; known: {sorted(_PROVIDERS)}. "
            f"Add an adapter in app/llm/ and register it here."
        )
    if cls is OpenAIProvider:
        return cls(model, base_url=base_url)
    return cls(model)
