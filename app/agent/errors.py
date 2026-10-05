"""Error taxonomy for the AI Opportunity Agent.

Every failure a stage raises is an `AgentError` carrying a `category` and a
stable `code`. The job runner uses ONLY the category to decide what happens
next, so retry behaviour is deterministic and testable:

    TRANSIENT, RATE_LIMIT, EXTERNAL_SERVICE, SYSTEM -> retry with backoff,
                                                      then DEAD_LETTER
    VALIDATION, DATA                                -> NEEDS_REVIEW (a human
                                                      looks at it; retrying
                                                      the same input will not
                                                      fix it)
    AUTHENTICATION                                  -> FAILED (config problem;
                                                      retrying burns quota)
"""

from __future__ import annotations

import enum


class ErrorCategory(str, enum.Enum):
    TRANSIENT = "transient"
    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    EXTERNAL_SERVICE = "external_service"
    DATA = "data"
    SYSTEM = "system"


RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMIT, ErrorCategory.EXTERNAL_SERVICE, ErrorCategory.SYSTEM}
)
NEEDS_REVIEW_CATEGORIES: frozenset[ErrorCategory] = frozenset({ErrorCategory.VALIDATION, ErrorCategory.DATA})


class AgentError(Exception):
    """Base error. `code` is a short, stable, log-friendly identifier
    (e.g. ``LLM_SCHEMA_VALIDATION_FAILED``); `message` must never contain
    secrets."""

    category: ErrorCategory = ErrorCategory.SYSTEM

    def __init__(self, message: str, *, code: str = "AGENT_ERROR", category: ErrorCategory | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        if category is not None:
            self.category = category

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, category={self.category.value!r}, message={self.message!r})"


class TransientError(AgentError):
    category = ErrorCategory.TRANSIENT


class RateLimitError(AgentError):
    category = ErrorCategory.RATE_LIMIT


class ExternalServiceError(AgentError):
    category = ErrorCategory.EXTERNAL_SERVICE


class AuthenticationError(AgentError):
    category = ErrorCategory.AUTHENTICATION


class ValidationFailedError(AgentError):
    category = ErrorCategory.VALIDATION


class DataError(AgentError):
    category = ErrorCategory.DATA


class ConfigurationError(AgentError):
    """Missing/invalid configuration. Treated like an auth problem: retrying
    cannot fix it, an operator must."""

    category = ErrorCategory.AUTHENTICATION


def classify_exception(exc: BaseException) -> AgentError:
    """Wrap any exception into an `AgentError` so the runner can decide.
    Unknown exceptions are SYSTEM (retryable, bounded) -- a bug should not
    silently disappear, and it must not loop forever either."""
    if isinstance(exc, AgentError):
        return exc
    if isinstance(exc, TimeoutError):
        return TransientError(f"timeout: {exc}", code="TIMEOUT")
    try:  # httpx is a hard dependency of the backend already.
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return TransientError(f"network error: {type(exc).__name__}", code="NETWORK_ERROR")
    except ImportError:  # pragma: no cover
        pass
    return AgentError(f"{type(exc).__name__}: {exc}", code="UNEXPECTED_ERROR", category=ErrorCategory.SYSTEM)


def category_for_http_status(status_code: int) -> ErrorCategory:
    """Shared HTTP status -> category mapping for every outbound adapter."""
    if status_code in (401, 403):
        return ErrorCategory.AUTHENTICATION
    if status_code == 402:
        # "Payment required" (e.g. OpenRouter out of credits): not fixable by retrying.
        return ErrorCategory.AUTHENTICATION
    if status_code == 429:
        return ErrorCategory.RATE_LIMIT
    if status_code in (408, 425):
        return ErrorCategory.TRANSIENT
    if status_code >= 500:
        return ErrorCategory.EXTERNAL_SERVICE
    if status_code in (400, 404, 413, 422):
        return ErrorCategory.DATA
    return ErrorCategory.EXTERNAL_SERVICE
