"""Contract tests for the OpenAI adapter.

The point of these is not OpenAI: it is that a second provider with a much
smaller capability set still satisfies the same protocol, so an agent written
against the contract keeps working.
"""

from __future__ import annotations

import pytest

from app.llm.base import (
    Attachment,
    Capability,
    CompletionRequest,
    LLMProvider,
    Message,
    ProviderError,
    Role,
    TextPart,
)
from app.llm.factory import get_provider
from app.llm.openai_provider import OpenAIProvider

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}


def _provider() -> OpenAIProvider:
    return OpenAIProvider("gpt-5.5", client=object())


def test_satisfies_the_protocol():
    assert isinstance(_provider(), LLMProvider)


def test_declares_only_structured_output():
    provider = _provider()
    assert provider.supports(Capability.STRUCTURED_OUTPUT)
    # False here routes the caller through degrade.py rather than failing.
    for cap in (
        Capability.PROMPT_CACHE,
        Capability.BATCH,
        Capability.PDF_INPUT,
        Capability.WEB_SEARCH,
        Capability.THINKING,
    ):
        assert not provider.supports(cap), cap


def test_system_parts_concatenate_into_instructions():
    kwargs = _provider().build_kwargs(
        CompletionRequest(
            system=[TextPart("framework", cacheable=True), TextPart("profile")],
            messages=[Message(Role.USER, "score this")],
        )
    )
    assert kwargs["instructions"] == "framework\n\nprofile"
    assert kwargs["input"][0]["content"][0]["text"] == "score this"


def test_no_cache_control_is_emitted():
    # The cacheable flag is meaningless here; emitting a breakpoint would be a
    # promise this provider cannot keep.
    kwargs = _provider().build_kwargs(
        CompletionRequest(system=[TextPart("a", cacheable=True)])
    )
    assert "cache_control" not in str(kwargs)


def test_schema_uses_the_documented_text_format_shape():
    kwargs = _provider().build_kwargs(CompletionRequest(output_schema=SCHEMA))
    fmt = kwargs["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["schema"] == SCHEMA
    assert fmt["name"]  # every example in the guide sets one


def test_no_schema_means_no_text_key():
    assert "text" not in _provider().build_kwargs(CompletionRequest())


def test_attachments_must_be_text_by_build_time():
    # complete() extracts PDFs first; reaching build_kwargs with one attached
    # means a caller went around that, which should be loud, not silent.
    with pytest.raises(ProviderError):
        _provider().build_kwargs(
            CompletionRequest(attachments=[Attachment("application/pdf", b"%PDF")])
        )


def test_factory_resolves_both_providers():
    assert get_provider("openai", "gpt-5.5").name == "openai"
    assert get_provider("anthropic", "claude-sonnet-5-5").name == "anthropic"


def test_factory_names_the_fix_for_an_unknown_provider():
    with pytest.raises(ValueError, match="unknown provider"):
        get_provider("gemini", "whatever")


def test_factory_passes_base_url_for_openai_compatible_endpoints():
    provider = get_provider("openai", "llama3", base_url="http://localhost:11434/v1")
    assert provider._base_url == "http://localhost:11434/v1"
