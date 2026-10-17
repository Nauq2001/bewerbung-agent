"""Contract tests for the provider layer.

Stdlib + pytest only: no SDK, no API key, no network. These are the tests that
must still pass the day someone adds a second provider.
"""

from __future__ import annotations

import json

import pytest

from app.llm import degrade
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import (
    Capability,
    CompletionRequest,
    Effort,
    LLMProvider,
    Message,
    Role,
    SchemaValidationError,
    TextPart,
    Usage,
)

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "skills": {"type": "array"}},
    "required": ["name", "skills"],
}


# --- cache prefix ----------------------------------------------------------


def test_cache_prefix_stops_at_first_volatile_part():
    req = CompletionRequest(
        system=[
            TextPart("framework", cacheable=True),
            TextPart("profile", cacheable=True),
            TextPart("today is 2026-10-17", cacheable=False),
            TextPart("never reached", cacheable=True),
        ]
    )
    # Caching is a prefix match, so a cacheable part after a volatile one
    # cannot be cached - counting it would promise a hit that never happens.
    assert req.cache_prefix_len() == 2


def test_cache_prefix_zero_when_first_part_is_volatile():
    req = CompletionRequest(system=[TextPart("volatile"), TextPart("x", cacheable=True)])
    assert req.cache_prefix_len() == 0


# --- anthropic request shape ----------------------------------------------


def test_breakpoint_lands_on_last_cacheable_part():
    provider = AnthropicProvider("claude-sonnet-5-5", client=object())
    kwargs = provider.build_kwargs(
        CompletionRequest(
            system=[
                TextPart("a", cacheable=True),
                TextPart("b", cacheable=True),
                TextPart("c"),
            ],
            messages=[Message(Role.USER, "hi")],
        )
    )
    system = kwargs["system"]
    assert "cache_control" not in system[0]
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[2]


def test_effort_maps_into_output_config():
    provider = AnthropicProvider("claude-opus-5-5", client=object())
    kwargs = provider.build_kwargs(CompletionRequest(effort=Effort.HIGH))
    assert kwargs["output_config"]["effort"] == "high"


def test_removed_parameters_are_never_sent():
    """budget_tokens and prefill are 400s on current models."""
    provider = AnthropicProvider("claude-opus-5-5", client=object())
    kwargs = provider.build_kwargs(CompletionRequest(thinking=True, effort=Effort.LOW))
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(kwargs)
    # Prefill would show up as a trailing assistant message.
    assert all(message["role"] == "user" for message in kwargs["messages"])


def test_schema_goes_to_output_config_when_supported():
    provider = AnthropicProvider("claude-haiku-5-5", client=object())
    kwargs = provider.build_kwargs(CompletionRequest(output_schema=SCHEMA))
    assert kwargs["output_config"]["format"]["schema"] == SCHEMA


def test_pdf_attachment_precedes_text():
    from app.llm.base import Attachment

    provider = AnthropicProvider("claude-haiku-5-5", client=object())
    kwargs = provider.build_kwargs(
        CompletionRequest(
            attachments=[Attachment("application/pdf", b"%PDF-1.4 fake")],
            messages=[Message(Role.USER, "extract")],
        )
    )
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "document"
    assert content[1]["type"] == "text"


# --- degradation -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"name": "Quan", "skills": ["sql"]}',
        'Here is the JSON:\n{"name": "Quan", "skills": ["sql"]}',
        '```json\n{"name": "Quan", "skills": ["sql"]}\n```',
        '```\n{"name": "Quan", "skills": ["sql"]}\n```\nHope that helps!',
    ],
)
def test_extract_json_survives_every_wrapper_models_use(raw):
    assert degrade.extract_json(raw)["name"] == "Quan"


def test_extract_json_rejects_prose():
    with pytest.raises(SchemaValidationError):
        degrade.extract_json("I cannot do that.")


def test_required_key_check_names_what_is_missing():
    with pytest.raises(SchemaValidationError, match="skills"):
        degrade.check_required({"name": "Quan"}, SCHEMA)


def test_schema_instruction_is_cacheable():
    # The schema is identical on every call for a given agent, so on a
    # provider with PROMPT_CACHE this must not re-bill each time.
    assert degrade.schema_instruction(SCHEMA).cacheable is True


def test_with_schema_in_prompt_appends_and_keeps_order():
    req = CompletionRequest(system=[TextPart("rules", cacheable=True)], output_schema=SCHEMA)
    out = degrade.with_schema_in_prompt(req)
    assert len(out.system) == 2
    assert "JSON Schema" in out.system[-1].text


def test_with_schema_in_prompt_is_noop_without_schema():
    req = CompletionRequest(system=[TextPart("rules")])
    assert len(degrade.with_schema_in_prompt(req).system) == 1


# --- protocol conformance --------------------------------------------------


class FakeProvider:
    """A second provider, to prove the contract is not Anthropic-shaped."""

    name = "fake"
    model = "fake-1"

    def supports(self, cap: Capability) -> bool:
        return cap is Capability.STRUCTURED_OUTPUT

    async def complete(self, req):  # pragma: no cover - shape check only
        from app.llm.base import CompletionResult

        return CompletionResult(text="{}", provider=self.name)


def test_both_providers_satisfy_the_protocol():
    assert isinstance(AnthropicProvider("claude-sonnet-5-5", client=object()), LLMProvider)
    assert isinstance(FakeProvider(), LLMProvider)


def test_capability_gaps_are_reported_not_hidden():
    fake = FakeProvider()
    assert fake.supports(Capability.STRUCTURED_OUTPUT)
    assert not fake.supports(Capability.PROMPT_CACHE)
    assert not fake.supports(Capability.PDF_INPUT)


def test_usage_reports_cache_hit():
    assert Usage(cache_read_tokens=1200).cache_hit
    assert not Usage(input_tokens=1200).cache_hit
