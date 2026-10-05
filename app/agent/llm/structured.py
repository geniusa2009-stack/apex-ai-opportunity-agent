"""Structured generation with bounded validation self-correction.

    attempt 1:  system + user                     -> text -> JSON -> Pydantic
    failure:    + assistant(previous output)
                + user("these validation errors ... return corrected JSON")
    attempt 2..: same, at most `max_validation_retries` extra attempts
    exhausted:  ValidationFailedError(code=LLM_SCHEMA_VALIDATION_FAILED)
                -> the job goes to NEEDS_REVIEW (a human looks at it)

This "self-healing" is limited to *data*: the model is asked to correct its
own JSON. Nothing here modifies code.

Fallback: if the PRIMARY provider fails with a transport-level error
(transient, rate limit, external service, auth), the same request is tried
once on the FALLBACK provider. A validation failure does not trigger the
fallback -- the error budget already applied.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.agent.errors import AgentError, ErrorCategory, ValidationFailedError
from app.agent.llm.base import ChatMessage, LLMProvider, LLMRequest
from app.agent.observability import log_stage

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S | re.I)
_MAX_ERRORS_FED_BACK = 12


@dataclasses.dataclass
class StructuredResult(Generic[T]):
    value: T
    provider: str
    model: str
    attempts: int
    raw_text: str


def parse_json_object(text: str) -> Any:
    """Strict-ish JSON extraction: accepts a bare object or one wrapped in a
    ```json fence; also tolerates prose around a single top-level object.
    Raises ValueError otherwise (never `eval`, never guessing)."""
    candidate = text.strip()
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("output contains no JSON object")
    return json.loads(candidate[start : end + 1])  # may raise JSONDecodeError (a ValueError)


def format_validation_errors(exc: ValidationError) -> list[str]:
    errors = []
    for err in exc.errors()[:_MAX_ERRORS_FED_BACK]:
        loc = ".".join(str(part) for part in err.get("loc", ())) or "(root)"
        errors.append(f"{loc}: {err.get('msg')}")
    return errors


def _correction_prompt(errors: list[str]) -> str:
    bullet = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous answer failed validation:\n"
        f"{bullet}\n\n"
        "Return ONLY the corrected JSON object. Keep every value that was valid. "
        "For any value you cannot support from the source text, use null -- never invent one."
    )


async def generate_structured(
    provider: LLMProvider,
    *,
    system: str,
    user: str,
    schema: type[T],
    max_validation_retries: int,
    temperature: float = 0.1,
    max_output_tokens: int = 2048,
    validation_context: dict[str, Any] | None = None,
    stage: str = "llm",
    fallback: LLMProvider | None = None,
) -> StructuredResult[T]:
    try:
        return await _generate_once(
            provider,
            system=system,
            user=user,
            schema=schema,
            max_validation_retries=max_validation_retries,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            validation_context=validation_context,
            stage=stage,
        )
    except AgentError as exc:
        if fallback is None or exc.category in (ErrorCategory.VALIDATION, ErrorCategory.DATA):
            raise
        log_stage(f"{stage}_fallback", provider=provider.name, fallback=fallback.name, error_code=exc.code)
        return await _generate_once(
            fallback,
            system=system,
            user=user,
            schema=schema,
            max_validation_retries=max_validation_retries,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            validation_context=validation_context,
            stage=stage,
        )


async def _generate_once(
    provider: LLMProvider,
    *,
    system: str,
    user: str,
    schema: type[T],
    max_validation_retries: int,
    temperature: float,
    max_output_tokens: int,
    validation_context: dict[str, Any] | None,
    stage: str,
) -> StructuredResult[T]:
    messages: list[ChatMessage] = [ChatMessage("user", user)]
    last_errors: list[str] = []
    total_attempts = 1 + max(0, max_validation_retries)
    for attempt in range(1, total_attempts + 1):
        response = await provider.complete(
            LLMRequest(system=system, messages=list(messages), temperature=temperature, max_output_tokens=max_output_tokens)
        )
        try:
            data = parse_json_object(response.text)
            value = schema.model_validate(data, context=validation_context or {})
        except ValidationError as exc:
            last_errors = format_validation_errors(exc)
        except ValueError as exc:  # includes JSONDecodeError
            last_errors = [f"(root): output is not a single valid JSON object ({exc})"]
        else:
            log_stage(f"{stage}_ok", provider=response.provider, model=response.model, attempts=attempt)
            return StructuredResult(value=value, provider=response.provider, model=response.model,
                                    attempts=attempt, raw_text=response.text)
        log_stage(
            f"{stage}_validation_failed",
            provider=response.provider,
            attempt=attempt,
            max_attempts=total_attempts,
            errors=last_errors[:3],
        )
        messages.append(ChatMessage("assistant", response.text[:8000]))
        messages.append(ChatMessage("user", _correction_prompt(last_errors)))
    raise ValidationFailedError(
        f"model output still invalid after {total_attempts} attempt(s): {'; '.join(last_errors[:5])}",
        code="LLM_SCHEMA_VALIDATION_FAILED",
    )
