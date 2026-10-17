"""Anthropic adapter.

This is one of the two places in the codebase allowed to know a vendor's name.
Everything vendor-specific lives behind the LLMProvider protocol:

  * ``effort`` -> ``output_config={"effort": ...}``
  * cacheable system parts -> ``cache_control`` breakpoint
  * ``output_schema`` -> ``output_config={"format": ...}``
  * ``thinking`` -> ``thinking={"type": "adaptive"}``

Three parameters that look plausible and are *rejected* by current models, so
they are never sent: ``budget_tokens`` (removed - use effort), assistant
prefill (400), and forced ``tool_choice`` of ``any``/``tool`` (400 - use
``auto`` plus an instruction, or a schema).
"""

from __future__ import annotations

from typing import Any

from app.llm import degrade
from app.llm.base import (
    Capability,
    CompletionRequest,
    CompletionResult,
    ProviderError,
    SchemaValidationError,
    Usage,
)

# Capabilities of the current Anthropic model line. Kept as a set rather than
# a per-model table because every model this project targets supports all of
# them; revisit when adding an older model.
_CAPS: frozenset[Capability] = frozenset(
    {
        Capability.STRUCTURED_OUTPUT,
        Capability.PROMPT_CACHE,
        Capability.BATCH,
        Capability.PDF_INPUT,
        Capability.WEB_SEARCH,
        Capability.THINKING,
    }
)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, client: Any | None = None) -> None:
        self.model = model
        self._client = client  # injected in tests; built lazily in production

    def supports(self, cap: Capability) -> bool:
        return cap in _CAPS

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(self.name, "anthropic SDK not installed") from exc
            # Credentials resolve from the environment (ANTHROPIC_API_KEY, or an
            # `ant auth login` profile) - never hardcoded here.
            self._client = AsyncAnthropic()
        return self._client

    def build_kwargs(self, req: CompletionRequest) -> dict[str, Any]:
        """Translate a CompletionRequest into Messages API kwargs.

        Pure and synchronous on purpose: the request shape is what regresses
        when the API drifts, and a pure function is cheap to unit-test without
        a key or a network.
        """
        prefix_len = req.cache_prefix_len()
        system: list[dict[str, Any]] = []
        for index, part in enumerate(req.system):
            block: dict[str, Any] = {"type": "text", "text": part.text}
            # One breakpoint, after the last contiguous cacheable part.
            if prefix_len and index == prefix_len - 1:
                block["cache_control"] = {"type": "ephemeral"}
            system.append(block)

        content: list[dict[str, Any]] = []
        for attachment in req.attachments:
            if attachment.media_type == "application/pdf":
                import base64

                content.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(attachment.data).decode(),
                        },
                    }
                )
        for message in req.messages:
            content.append({"type": "text", "text": message.content})

        output_config: dict[str, Any] = {"effort": req.effort.value}
        if req.output_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": req.output_schema,
            }

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": req.max_tokens,
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config,
        }
        if system:
            kwargs["system"] = system
        if req.thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if req.tools:
            kwargs["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "strict": True,
                }
                for tool in req.tools
            ]
        return kwargs

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        kwargs = self.build_kwargs(req)
        try:
            response = await self.client.messages.create(**kwargs)
        except Exception as exc:  # adapter boundary: normalize to ProviderError
            raise ProviderError(self.name, str(exc)) from exc

        text = "".join(
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", None) == "text"
        )

        parsed = None
        if req.output_schema is not None:
            parsed = degrade.extract_json(text)
            degrade.check_required(parsed, req.output_schema)

        raw_usage = getattr(response, "usage", None)
        usage = Usage(
            input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(raw_usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(raw_usage, "cache_creation_input_tokens", 0) or 0,
        )

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise SchemaValidationError(
                self.name, f"refused: {getattr(details, 'category', 'unknown')}"
            )

        return CompletionResult(
            text=text,
            parsed=parsed,
            usage=usage,
            model=getattr(response, "model", self.model),
            provider=self.name,
            stop_reason=stop_reason,
        )
