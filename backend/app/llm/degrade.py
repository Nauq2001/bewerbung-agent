"""Emulation for capabilities a provider lacks.

Every function here answers the same question: the agent asked for something
this provider cannot do natively - what is the cheapest honest substitute?

None of these raise on a missing capability. A provider swap must change the
bill and sometimes the accuracy, never the set of features that work.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.llm.base import CompletionRequest, SchemaValidationError, TextPart

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def schema_instruction(schema: dict[str, Any]) -> TextPart:
    """System part that asks for schema-valid JSON in prose.

    Used when the provider has no STRUCTURED_OUTPUT. Marked cacheable because
    the schema is identical on every call for a given agent, so on a provider
    that does have PROMPT_CACHE this costs almost nothing after the first call.
    """
    return TextPart(
        text=(
            "Respond with a single JSON object and nothing else. No prose, no "
            "code fence, no explanation. The object must validate against this "
            "JSON Schema:\n\n" + json.dumps(schema, indent=2, sort_keys=True)
        ),
        cacheable=True,
    )


def with_schema_in_prompt(req: CompletionRequest) -> CompletionRequest:
    """Move ``output_schema`` from the request into the system prompt."""
    if req.output_schema is None:
        return req
    req.system = [*req.system, schema_instruction(req.output_schema)]
    return req


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Models fenced it, prefixed it with "Here is the JSON:", or returned it
    bare. Try the cheap paths before the expensive one.
    """
    candidates: list[str] = []

    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())

    # Outermost brace pair: survives leading prose.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise SchemaValidationError("degrade", f"no JSON object in response: {text[:200]!r}")


def check_required(parsed: dict[str, Any], schema: dict[str, Any]) -> None:
    """Shallow required-key check.

    Deliberately not a full JSON Schema validator. The agents own real
    validation via their pydantic models; this exists so the retry loop in
    ``call_with_schema`` can tell "model forgot a field" from "model returned
    prose", which are different failures with different fixes.
    """
    missing = [key for key in schema.get("required", []) if key not in parsed]
    if missing:
        raise SchemaValidationError(
            "degrade", f"response missing required keys: {', '.join(sorted(missing))}"
        )


def pdf_to_text(data: bytes) -> str:
    """Local text extraction, for providers without PDF_INPUT.

    Loses layout, so Agent 1's accuracy drops - that is the documented cost of
    the degradation, not a bug to paper over.
    """
    try:
        from pypdf import PdfReader  # imported lazily: only this path needs it
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise SchemaValidationError(
            "degrade", "provider lacks PDF_INPUT and pypdf is not installed"
        ) from exc

    import io

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)
