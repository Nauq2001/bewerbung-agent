"""Provider-agnostic contract for every LLM call in this project.

Deliberately stdlib-only: no pydantic, no SDK imports. Two reasons.

  * The unit tests for this layer must run without installing anything, so a
    contract change is caught in milliseconds.
  * Importing a vendor SDK here would make the vendor part of the contract,
    which is the exact coupling this module exists to prevent.

The rule that keeps this layer honest: callers branch on
``provider.supports(Capability.X)``, never on ``provider.name``. A
``if provider.name == "anthropic"`` anywhere outside ``app/llm/<vendor>_*.py``
is the lock-in coming back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Capability(StrEnum):
    """What a provider can do natively.

    Anything a provider lacks is emulated by its adapter (see app/llm/degrade.py)
    at some cost in tokens, latency or accuracy - never by failing the call.
    """

    STRUCTURED_OUTPUT = "structured_output"
    PROMPT_CACHE = "prompt_cache"
    BATCH = "batch"
    PDF_INPUT = "pdf_input"
    WEB_SEARCH = "web_search"
    THINKING = "thinking"


class Effort(StrEnum):
    """Normalized reasoning depth.

    Each adapter maps these onto its own vendor's knob. Three levels, not five:
    a level that some providers cannot express is a level the contract cannot
    promise.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class TextPart:
    """A chunk of system prompt.

    ``cacheable`` marks a prefix-stable chunk. Providers with
    ``Capability.PROMPT_CACHE`` place a cache breakpoint after the last
    cacheable part; the rest ignore the flag. Order matters: stable parts
    first, volatile parts last, because caching is a prefix match.
    """

    text: str
    cacheable: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file sent with the request.

    ``data`` is raw bytes. Adapters for providers without
    ``Capability.PDF_INPUT`` extract text locally first.
    """

    media_type: str
    data: bytes
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class CompletionRequest:
    system: list[TextPart] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    max_tokens: int = 4096
    effort: Effort = Effort.MEDIUM
    # JSON Schema the response must satisfy. Providers with
    # STRUCTURED_OUTPUT enforce it server-side; the rest fall back to
    # schema-in-prompt plus validation (app/llm/degrade.py).
    output_schema: dict[str, Any] | None = None
    attachments: list[Attachment] = field(default_factory=list)
    tools: list[ToolSpec] = field(default_factory=list)
    thinking: bool = False

    def cache_prefix_len(self) -> int:
        """Number of leading system parts that form the cacheable prefix.

        Stops at the first non-cacheable part: once volatile text appears,
        nothing after it can be cached, because the match is a prefix match.
        """
        n = 0
        for part in self.system:
            if not part.cacheable:
                break
            n += 1
        return n


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def cache_hit(self) -> bool:
        return self.cache_read_tokens > 0


@dataclass(frozen=True, slots=True)
class CompletionResult:
    text: str
    # Populated only when the request carried an output_schema and the
    # response validated against it.
    parsed: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    stop_reason: str | None = None
    # True when the adapter emulated a capability the provider lacks. Worth
    # logging: it usually means the call cost more than it needed to.
    degraded: tuple[Capability, ...] = ()


class ProviderError(RuntimeError):
    """A call failed in a way the caller cannot retry its way out of."""

    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(f"{provider}: {detail}")
        self.provider = provider
        self.detail = detail


class SchemaValidationError(ProviderError):
    """The model returned output that does not satisfy ``output_schema``."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    def supports(self, cap: Capability) -> bool: ...

    async def complete(self, req: CompletionRequest) -> CompletionResult: ...
