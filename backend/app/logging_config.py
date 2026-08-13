import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO


SAFE_OPERATIONAL_FIELDS = (
    "gemini_operation",
    "gemini_model",
    "gemini_location",
    "gemini_max_attempts",
    "attempt_number",
    "max_attempts",
    "attempt_elapsed_ms",
    "attempt_success",
    "exception_type",
    "retryable",
    "retry_delay_ms",
    "claim_id",
    "document_id",
    "correlation_id",
    "elapsed_ms",
    "gemini_success",
    "provider_status_code",
    "provider_exception_type",
    "prompt_token_count",
    "candidates_token_count",
    "total_token_count",
    "cached_content_token_count",
    "thoughts_token_count",
)

_HANDLER_MARKER = "_firstnotice_cloud_run_json_handler"


class SafeJsonFormatter(logging.Formatter):
    """Serialize only explicitly approved operational logging fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "message": record.getMessage(),
            "severity": record.levelname,
            "logger": record.name,
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        for field in SAFE_OPERATIONAL_FIELDS:
            value = getattr(record, field, None)
            if value is not None and isinstance(value, (str, int, float, bool)):
                payload[field] = value
        if record.exc_info:
            payload["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def configure_application_logging(stream: TextIO | None = None) -> None:
    """Configure one INFO-level JSON handler for FirstNotice application logs."""
    application_logger = logging.getLogger("app")
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False

    if any(
        getattr(handler, _HANDLER_MARKER, False)
        for handler in application_logger.handlers
    ):
        return

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setLevel(logging.INFO)
    handler.setFormatter(SafeJsonFormatter())
    setattr(handler, _HANDLER_MARKER, True)
    application_logger.addHandler(handler)
