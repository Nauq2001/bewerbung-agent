"""OpenAI adapter - and the proof the provider layer was worth building.

Written so someone who only has an OpenAI key can run this project. It also
covers every OpenAI-compatible endpoint (Ollama, Together, Groq, vLLM) by way
of ``base_url``, which is why there is no separate adapter for those.

What it supports natively is deliberately narrow: structured output. Everything
else falls through to app/llm/degrade.py, which is the whole point - the
feature set does not shrink, the bill and the PDF accuracy do.
"""

from __future__ import annotations

from typing import Any

from app.llm import degrade
from app.llm.base import (
    Capability,
    CompletionRequest,
    CompletionResult,
    ProviderError,
    Usage,
)

_CAPS: frozenset[Capability] = frozenset({Capability.STRUCTURED_OUTPUT})


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str, client: Any | None = None, base_url: str | None = None) -> None:
        self.model = model
        self._client = client
        self._base_url = base_url

    def supports(self, cap: Capability) -> bool:
        # PROMPT_CACHE is False on purpose, not as an oversight: OpenAI caches
        # prefixes automatically with no breakpoint to place, so this adapter
        # has nothing to declare. Ordering stable system parts first still
        # helps; we just cannot promise a hit.
        # BATCH, PDF_INPUT, WEB_SEARCH and THINKING are False because this
        # adapter does not implement them, so degrade.py handles them.
        return cap in _CAPS

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover
                raise ProviderError(self.name, "openai SDK not installed") from exc
            # Key and base URL resolve from OPENAI_API_KEY / OPENAI_BASE_URL
            # unless base_url is given explicitly.
            self._client = (
                AsyncOpenAI(base_url=self._base_url) if self._base_url else AsyncOpenAI()
            )
        return self._client

    def build_kwargs(self, req: CompletionRequest) -> dict[str, Any]:
        """Translate a CompletionRequest into Responses API kwargs.

        Pure and synchronous, same as the Anthropic adapter: the request shape
        is what breaks when a vendor's API drifts, and a pure function is
        testable without a key.
        """
        # No cache_control to place, so system parts just concatenate in order.
        instructions = "\n\n".join(part.text for part in req.system)

        # No PDF_INPUT capability: complete() extracts text first, so anything
        # still attached here is a caller bypassing it.
        if req.attachments:
            raise ProviderError(
                self.name, "attachments must be converted to text before build_kwargs"
            )

        content = [{"type": "input_text", "text": m.content} for m in req.messages]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": [{"role": "user", "content": content}],
            # shortcut: `max_output_tokens` and an effort/reasoning parameter
            # could not be verified - platform.openai.com's Chat Completions
            # reference returns 403 to fetchers and the SDK README documents
            # neither. If a call fails with an unknown-parameter 400, fix the
            # name here; the ProviderError carries the API's own message.
            "max_output_tokens": req.max_tokens,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if req.output_schema is not None:
            # Verbatim shape from OpenAI's structured-outputs guide. The schema
            # must set additionalProperties:false and list every property in
            # required - an optional field is spelled as a null union instead.
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "response",
                    "schema": req.output_schema,
                    "strict": True,
                }
            }
        return kwargs

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        degraded: list[Capability] = []

        # Fold PDFs into text locally, since this provider takes none.
        if req.attachments:
            extracted = [
                degrade.pdf_to_text(a.data)
                for a in req.attachments
                if a.media_type == "application/pdf"
            ]
            if extracted:
                from app.llm.base import Message, Role

                req.messages = [
                    Message(Role.USER, "\n\n".join(extracted)),
                    *req.messages,
                ]
                req.attachments = []
                degraded.append(Capability.PDF_INPUT)

        kwargs = self.build_kwargs(req)
        try:
            response = await self.client.responses.create(**kwargs)
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc

        text = getattr(response, "output_text", "") or ""

        parsed = None
        if req.output_schema is not None:
            parsed = degrade.extract_json(text)
            degrade.check_required(parsed, req.output_schema)

        raw = getattr(response, "usage", None)
        details = getattr(raw, "input_tokens_details", None)
        usage = Usage(
            input_tokens=getattr(raw, "input_tokens", 0) or 0,
            output_tokens=getattr(raw, "output_tokens", 0) or 0,
            # Reported if present; unverified field name, hence the default.
            cache_read_tokens=getattr(details, "cached_tokens", 0) or 0,
        )

        return CompletionResult(
            text=text,
            parsed=parsed,
            usage=usage,
            model=getattr(response, "model", self.model),
            provider=self.name,
            stop_reason=getattr(response, "status", None),
            degraded=tuple(degraded),
        )
