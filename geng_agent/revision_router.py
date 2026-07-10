from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator


DEFAULT_MAX_REENTRIES = 3

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ErrorCategory(str, Enum):
    ANALYSIS_SCOPE = "analysis_scope"
    CONTRACT_ERROR = "contract_error"
    CODE_OR_RUNTIME = "code_or_runtime"
    ENVIRONMENT = "environment"


class RevisionRequest(BaseModel):
    """Code-local contract for a request sent back to a task worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: NonEmptyStr
    scenario: NonEmptyStr
    error: NonEmptyStr | dict[str, Any]
    requested_changes: list[NonEmptyStr] = Field(min_length=1)
    reentry_count: int = Field(default=0, ge=0, strict=True)
    category: ErrorCategory | None = None

    @field_validator("error")
    @classmethod
    def validate_error_payload(cls, value: str | dict[str, Any]) -> str | dict[str, Any]:
        if isinstance(value, dict) and not value:
            raise ValueError("error object must not be empty")
        if isinstance(value, dict):
            try:
                json.dumps(value, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("error object must contain finite JSON values") from exc
        return value


def validate_revision_request(data: Any) -> list[dict[str, str]]:
    """Validate request structure without raising, returning JSON-path issues."""

    if not isinstance(data, Mapping):
        return [{"path": "$", "message": "revision request must be an object"}]
    try:
        RevisionRequest.model_validate(dict(data))
    except ValidationError as exc:
        return _validation_issues(exc)
    return []


def parse_revision_request(data: Mapping[str, Any]) -> RevisionRequest:
    """Validate and return the immutable request model, raising on invalid input."""

    return RevisionRequest.model_validate(dict(data))


def classify_revision_error(
    error: RevisionRequest | BaseException | Mapping[str, Any] | str,
    *,
    explicit_category: ErrorCategory | str | None = None,
) -> ErrorCategory:
    """Classify a revision cause into the four routing categories.

    An explicit valid category wins. Otherwise specific environment and contract
    signals are checked before broad runtime terms. Unknown failures conservatively
    route to ``code_or_runtime`` so they remain actionable.
    """

    if isinstance(error, RevisionRequest):
        explicit_category = explicit_category or error.category
        error = error.error
    if explicit_category is None and isinstance(error, Mapping):
        explicit_category = error.get("category") or error.get("error_category")
    if explicit_category is not None:
        try:
            if isinstance(explicit_category, ErrorCategory):
                return explicit_category
            return ErrorCategory(explicit_category)
        except ValueError as exc:
            allowed = ", ".join(category.value for category in ErrorCategory)
            raise ValueError(f"unknown error category {explicit_category!r}; expected one of {allowed}") from exc

    if isinstance(error, BaseException):
        exception_category = _classify_exception(error)
        if exception_category is not None:
            return exception_category
    text = _error_text(error)
    for category in (
        ErrorCategory.ENVIRONMENT,
        ErrorCategory.CONTRACT_ERROR,
        ErrorCategory.ANALYSIS_SCOPE,
        ErrorCategory.CODE_OR_RUNTIME,
    ):
        if any(pattern.search(text) for pattern in _CATEGORY_PATTERNS[category]):
            return category
    return ErrorCategory.CODE_OR_RUNTIME


def can_reenter(
    request_or_count: RevisionRequest | Mapping[str, Any] | int,
    *,
    max_reentries: int = DEFAULT_MAX_REENTRIES,
) -> bool:
    """Return whether one more worker re-entry is allowed.

    ``reentry_count`` is the number already consumed, so equality with the limit
    is denied. Booleans are rejected even though ``bool`` subclasses ``int``.
    """

    _validate_count(max_reentries, name="max_reentries")
    if isinstance(request_or_count, RevisionRequest):
        count = request_or_count.reentry_count
    elif isinstance(request_or_count, Mapping):
        count = request_or_count.get("reentry_count", 0)
    else:
        count = request_or_count
    _validate_count(count, name="reentry_count")
    return count < max_reentries


def _validate_count(value: Any, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _validation_issues(exc: ValidationError) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for error in exc.errors():
        path = "$"
        for part in error.get("loc", ()):
            path += f"[{part}]" if isinstance(part, int) else f".{part}"
        issue = (path, str(error.get("msg", "invalid value")))
        if issue in seen:
            continue
        seen.add(issue)
        issues.append({"path": issue[0], "message": issue[1]})
    return issues


def _classify_exception(error: BaseException) -> ErrorCategory | None:
    if isinstance(error, (ModuleNotFoundError, PermissionError, OSError)):
        return ErrorCategory.ENVIRONMENT
    if isinstance(error, (SyntaxError, RuntimeError, AssertionError)):
        return ErrorCategory.CODE_OR_RUNTIME
    if isinstance(error, (TypeError, ValueError, KeyError)):
        return ErrorCategory.CONTRACT_ERROR
    return None


def _error_text(error: Mapping[str, Any] | str | BaseException) -> str:
    if isinstance(error, Mapping):
        raw = json.dumps(dict(error), ensure_ascii=False, sort_keys=True, default=str)
    else:
        raw = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    return re.sub(r"\s+", " ", raw).strip().lower()


def _patterns(*expressions: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(expression, re.IGNORECASE) for expression in expressions)


_CATEGORY_PATTERNS = {
    ErrorCategory.ENVIRONMENT: _patterns(
        r"\b(?:module|package|dependency)\s+(?:is\s+)?not\s+(?:found|installed|available)\b",
        r"\b(?:modulenotfounderror|no module named)\b",
        r"\b(?:cuda|gpu|driver)\s+(?:is\s+)?(?:unavailable|not available|missing|unsupported)\b",
        r"\b(?:permission denied|access denied|read-only file system|disk full|out of memory)\b",
        r"\b(?:network|proxy|dns|connection)\s+(?:is\s+)?(?:unavailable|failed|refused|timed out)\b",
        r"\b(?:environment|infrastructure|resource limit)\b",
    ),
    ErrorCategory.CONTRACT_ERROR: _patterns(
        r"\b(?:schema|contract|validation)\b",
        r"\b(?:missing|required|unknown|extra)\s+(?:field|key|property|artifact)\b",
        r"\b(?:invalid|malformed)\s+(?:json|format|type|payload|request)\b",
        r"\bdoes not match (?:the )?(?:contract|schema|assigned task)\b",
    ),
    ErrorCategory.ANALYSIS_SCOPE: _patterns(
        r"\b(?:analysis|task|request)\s+(?:is\s+)?(?:out of scope|too broad|underspecified|ambiguous)\b",
        r"\b(?:insufficient|missing|unclear)\s+(?:paper )?(?:evidence|information|context|requirements?)\b",
        r"\b(?:unsupported|unverifiable)\s+(?:claim|assumption|scenario|scope)\b",
        r"\bcannot assess\b",
    ),
    ErrorCategory.CODE_OR_RUNTIME: _patterns(
        r"\b(?:syntaxerror|typeerror|nameerror|attributeerror|assertionerror|traceback)\b",
        r"\b(?:compile|execution|runtime|script|test)\s+(?:error|failure|failed|crashed|timed out)\b",
        r"\b(?:non[- ]?zero exit|return code|segmentation fault|bug)\b",
    ),
}
