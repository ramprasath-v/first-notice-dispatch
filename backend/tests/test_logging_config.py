import io
import json
import logging

from app.logging_config import configure_application_logging


def _application_handlers() -> list[logging.Handler]:
    return list(logging.getLogger("app").handlers)


def _restore_logger(
    handlers: list[logging.Handler], level: int, propagate: bool
) -> None:
    logger = logging.getLogger("app")
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = propagate


def test_info_record_is_safe_structured_json_without_null_or_arbitrary_fields() -> None:
    logger = logging.getLogger("app.tools.gemini_client")
    app_logger = logging.getLogger("app")
    original = (_application_handlers(), app_logger.level, app_logger.propagate)
    output = io.StringIO()
    try:
        app_logger.handlers.clear()
        configure_application_logging(output)

        logger.info(
            "Gemini provider call completed.",
            extra={
                "gemini_operation": "claim_review",
                "gemini_model": "configured-model",
                "gemini_location": "global",
                "gemini_max_attempts": 3,
                "attempt_number": 2,
                "max_attempts": 3,
                "attempt_elapsed_ms": 30012.5,
                "attempt_success": False,
                "exception_type": "ServerError",
                "retryable": True,
                "retry_delay_ms": 1200,
                "claim_id": "CLM-SAFE",
                "document_id": None,
                "correlation_id": "CORR-SAFE",
                "elapsed_ms": 123.45,
                "gemini_success": True,
                "provider_status_code": 200,
                "total_token_count": 42,
                "raw_prompt": "must not be serialized",
                "policy_number": "must not be serialized",
            },
        )

        payload = json.loads(output.getvalue())
        assert payload["message"] == "Gemini provider call completed."
        assert payload["severity"] == "INFO"
        assert payload["logger"] == "app.tools.gemini_client"
        assert payload["timestamp"].endswith("Z")
        assert payload["gemini_operation"] == "claim_review"
        assert payload["attempt_number"] == 2
        assert payload["max_attempts"] == 3
        assert payload["attempt_elapsed_ms"] == 30012.5
        assert payload["attempt_success"] is False
        assert payload["exception_type"] == "ServerError"
        assert payload["retryable"] is True
        assert payload["retry_delay_ms"] == 1200
        assert payload["claim_id"] == "CLM-SAFE"
        assert payload["elapsed_ms"] == 123.45
        assert payload["total_token_count"] == 42
        assert "document_id" not in payload
        assert "raw_prompt" not in payload
        assert "policy_number" not in payload
        assert "must not be serialized" not in output.getvalue()
    finally:
        _restore_logger(*original)


def test_warning_and_exception_traceback_are_visible() -> None:
    logger = logging.getLogger("app.test")
    app_logger = logging.getLogger("app")
    original = (_application_handlers(), app_logger.level, app_logger.propagate)
    output = io.StringIO()
    try:
        app_logger.handlers.clear()
        configure_application_logging(output)
        logger.warning("Provider is temporarily unavailable.")
        try:
            raise RuntimeError("safe failure")
        except RuntimeError:
            logger.exception("Application operation failed.")

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        assert records[0]["severity"] == "WARNING"
        assert records[1]["severity"] == "ERROR"
        assert "RuntimeError: safe failure" in records[1]["stack_trace"]
    finally:
        _restore_logger(*original)


def test_configuration_is_idempotent_and_does_not_duplicate_output() -> None:
    logger = logging.getLogger("app.test")
    app_logger = logging.getLogger("app")
    original = (_application_handlers(), app_logger.level, app_logger.propagate)
    output = io.StringIO()
    try:
        app_logger.handlers.clear()
        configure_application_logging(output)
        configure_application_logging(output)
        logger.info("One record.")

        assert len(app_logger.handlers) == 1
        assert len(output.getvalue().splitlines()) == 1
        assert app_logger.propagate is False
    finally:
        _restore_logger(*original)
