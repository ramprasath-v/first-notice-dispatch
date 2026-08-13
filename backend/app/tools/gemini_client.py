import logging
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx
from google import genai
from google.genai import types

from app.config import Settings


logger = logging.getLogger(__name__)

GEMINI_TIMEOUT_MS = 30_000
GEMINI_MAX_ATTEMPTS = 1
GEMINI_TRANSIENT_STATUS_CODES = (408, 429, 500, 502, 503, 504)


@dataclass
class _AttemptContext:
    fields: dict[str, object]
    attempt_number: int = 0
    attempt_started: float | None = None
    failure_logged: bool = False


_attempt_context: ContextVar[_AttemptContext | None] = ContextVar(
    "gemini_attempt_context", default=None
)


def create_gemini_client(settings: Settings) -> genai.Client:
    """Create one ADC-authenticated google-genai client for Vertex AI."""
    client = genai.Client(
        vertexai=True,
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
        http_options=types.HttpOptions(
            timeout=GEMINI_TIMEOUT_MS,
        ),
    )
    _install_attempt_logging(client)
    return client


def observed_generate_content(
    client: Any,
    *,
    operation: str,
    model: str,
    location: str,
    claim_id: str | None = None,
    document_id: str | None = None,
    correlation_id: str | None = None,
    **request: Any,
) -> Any:
    """Call Gemini once while recording safe timing and provider metadata."""
    started = perf_counter()
    fields: dict[str, object] = {
        "gemini_operation": operation,
        "gemini_model": model,
        "gemini_location": location,
        "gemini_max_attempts": GEMINI_MAX_ATTEMPTS,
        "claim_id": claim_id,
        "document_id": document_id,
        "correlation_id": correlation_id,
    }
    attempt_context = _AttemptContext(fields=fields, attempt_started=started)
    token = _attempt_context.set(attempt_context)
    try:
        response = client.models.generate_content(model=model, **request)
    except Exception as exc:
        _log_final_failed_attempt(attempt_context, exc)
        logger.warning(
            "Gemini provider call completed.",
            extra={
                **fields,
                "elapsed_ms": round((perf_counter() - started) * 1000, 2),
                "gemini_success": False,
                "provider_status_code": _provider_status_code(exc),
                "provider_exception_type": type(exc).__name__,
            },
        )
        raise
    finally:
        _attempt_context.reset(token)

    _log_successful_attempt(attempt_context)
    logger.info(
        "Gemini provider call completed.",
        extra={
            **fields,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
            "gemini_success": True,
            "provider_status_code": 200,
            **_usage_fields(response),
        },
    )
    return response


def _install_attempt_logging(client: Any) -> None:
    """Attach safe callbacks to the SDK's existing retry executor."""
    api_client = getattr(client, "_api_client", None)
    retry = getattr(api_client, "_retry", None)
    if retry is None or getattr(retry, "_firstnotice_attempt_logging", False):
        return
    retry.before = _before_provider_attempt
    retry.before_sleep = _before_provider_retry_sleep
    setattr(retry, "_firstnotice_attempt_logging", True)


def _before_provider_attempt(retry_state: Any) -> None:
    context = _attempt_context.get()
    if context is None:
        return
    context.attempt_number = int(retry_state.attempt_number)
    context.attempt_started = perf_counter()
    context.failure_logged = False


def _before_provider_retry_sleep(retry_state: Any) -> None:
    context = _attempt_context.get()
    if context is None:
        return
    exception = retry_state.outcome.exception()
    delay = getattr(retry_state.next_action, "sleep", None)
    _log_attempt(
        context,
        success=False,
        exception=exception,
        retryable=True,
        retry_delay_ms=(round(delay * 1000, 2) if delay is not None else None),
    )
    context.failure_logged = True


def _log_successful_attempt(context: _AttemptContext) -> None:
    if context.attempt_number == 0:
        context.attempt_number = 1
        context.attempt_started = context.attempt_started or perf_counter()
    _log_attempt(context, success=True, provider_status_code=200, retryable=False)


def _log_final_failed_attempt(
    context: _AttemptContext, exception: Exception
) -> None:
    if context.failure_logged:
        return
    if context.attempt_number == 0:
        context.attempt_number = 1
        context.attempt_started = context.attempt_started or perf_counter()
    _log_attempt(
        context,
        success=False,
        exception=exception,
        retryable=_is_retryable_provider_error(exception),
    )


def _log_attempt(
    context: _AttemptContext,
    *,
    success: bool,
    retryable: bool,
    provider_status_code: int | None = None,
    exception: Exception | None = None,
    retry_delay_ms: float | None = None,
) -> None:
    started = context.attempt_started or perf_counter()
    logger.log(
        logging.INFO if success else logging.WARNING,
        "Gemini provider attempt completed.",
        extra={
            **context.fields,
            "attempt_number": context.attempt_number,
            "max_attempts": GEMINI_MAX_ATTEMPTS,
            "attempt_elapsed_ms": round((perf_counter() - started) * 1000, 2),
            "attempt_success": success,
            "provider_status_code": (
                provider_status_code
                if provider_status_code is not None
                else _provider_status_code(exception) if exception else None
            ),
            "exception_type": type(exception).__name__ if exception else None,
            "retryable": retryable,
            "retry_delay_ms": retry_delay_ms,
        },
    )


def _is_retryable_provider_error(exc: Exception) -> bool:
    status_code = _provider_status_code(exc)
    return status_code in GEMINI_TRANSIENT_STATUS_CODES or isinstance(
        exc, (httpx.TimeoutException, httpx.ConnectError)
    )


def _provider_status_code(exc: Exception) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if isinstance(code, int):
            return code
        response = getattr(current, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        current = current.__cause__ or current.__context__
    return None


def _usage_fields(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}
    fields = {}
    for name in (
        "prompt_token_count",
        "candidates_token_count",
        "total_token_count",
        "cached_content_token_count",
        "thoughts_token_count",
    ):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            fields[name] = value
    return fields
