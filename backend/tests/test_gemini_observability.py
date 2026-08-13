import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import tenacity

from app.tools.gemini_client import (
    GEMINI_MAX_ATTEMPTS,
    GEMINI_TRANSIENT_STATUS_CODES,
    _install_attempt_logging,
    observed_generate_content,
)


class ProviderError(RuntimeError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def retrying_client(*outcomes):
    remaining = list(outcomes)

    def provider_call():
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    retry = tenacity.Retrying(
        stop=tenacity.stop_after_attempt(GEMINI_MAX_ATTEMPTS),
        retry=tenacity.retry_if_exception(
            lambda exc: (
                isinstance(exc, ProviderError)
                and exc.code in GEMINI_TRANSIENT_STATUS_CODES
            )
        ),
        wait=tenacity.wait_none(),
        sleep=lambda _: None,
        reraise=True,
    )
    client = SimpleNamespace(
        _api_client=SimpleNamespace(_retry=retry),
        models=SimpleNamespace(generate_content=lambda **_: retry(provider_call)),
    )
    _install_attempt_logging(client)
    return client


def attempt_records(caplog):
    return [
        record
        for record in caplog.records
        if record.getMessage() == "Gemini provider attempt completed."
    ]


def test_success_log_contains_safe_context_timing_and_usage(caplog) -> None:
    client = MagicMock()
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=4,
            total_token_count=14,
        )
    )
    client.models.generate_content.return_value = response

    with caplog.at_level(logging.INFO, logger="app.tools.gemini_client"):
        result = observed_generate_content(
            client,
            operation="document_extraction",
            model="configured-model",
            location="global",
            claim_id="CLM-SAFE",
            document_id="DOC-SAFE",
            correlation_id="CORR-SAFE",
            contents="sensitive prompt that must not be logged",
        )

    assert result is response
    record = caplog.records[-1]
    assert record.gemini_operation == "document_extraction"
    assert record.gemini_model == "configured-model"
    assert record.gemini_location == "global"
    assert record.claim_id == "CLM-SAFE"
    assert record.document_id == "DOC-SAFE"
    assert record.correlation_id == "CORR-SAFE"
    assert record.gemini_success is True
    assert record.provider_status_code == 200
    assert record.elapsed_ms >= 0
    assert record.total_token_count == 14
    assert "sensitive prompt" not in caplog.text
    attempts = attempt_records(caplog)
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].attempt_success is True


def test_failure_is_logged_once_without_sensitive_exception_text(caplog) -> None:
    client = MagicMock()
    client.models.generate_content.side_effect = ProviderError(
        "policy POL-SECRET VIN-SECRET plate SECRET", 429
    )

    with caplog.at_level(logging.WARNING, logger="app.tools.gemini_client"):
        with pytest.raises(ProviderError):
            observed_generate_content(
                client,
                operation="claim_review",
                model="configured-model",
                location="global",
                claim_id="CLM-SAFE",
                contents="raw evidence must not be logged",
            )

    client.models.generate_content.assert_called_once()
    record = caplog.records[-1]
    assert record.gemini_success is False
    assert record.provider_status_code == 429
    assert record.provider_exception_type == "ProviderError"
    assert "POL-SECRET" not in caplog.text
    assert "VIN-SECRET" not in caplog.text
    assert "raw evidence" not in caplog.text


def test_retryable_failure_does_not_consume_a_second_outcome(caplog) -> None:
    response = SimpleNamespace(usage_metadata=None)
    client = retrying_client(ProviderError("secret response", 504), response)

    with caplog.at_level(logging.INFO, logger="app.tools.gemini_client"):
        with pytest.raises(ProviderError):
            observed_generate_content(
                client,
                operation="claim_review",
                model="configured-model",
                location="global",
                claim_id="CLM-SAFE",
                contents="sensitive prompt",
            )

    attempts = attempt_records(caplog)
    assert [record.attempt_number for record in attempts] == [1]
    assert attempts[0].attempt_success is False
    assert attempts[0].provider_status_code == 504
    assert attempts[0].retryable is True
    assert attempts[0].retry_delay_ms is None
    assert attempts[0].max_attempts == 1
    assert len([
        record for record in caplog.records
        if record.getMessage() == "Gemini provider call completed."
    ]) == 1
    assert "secret response" not in caplog.text
    assert "sensitive prompt" not in caplog.text


def test_single_attempt_exhaustion_logs_attempt_and_aggregate_failure(caplog) -> None:
    client = retrying_client(
        ProviderError("secret-1", 504),
        ProviderError("secret-2", 504),
    )

    with caplog.at_level(logging.INFO, logger="app.tools.gemini_client"):
        with pytest.raises(ProviderError):
            observed_generate_content(
                client,
                operation="claim_review",
                model="configured-model",
                location="global",
                document_id="DOC-SAFE",
                contents="sensitive prompt",
            )

    attempts = attempt_records(caplog)
    assert [record.attempt_number for record in attempts] == [1]
    assert all(record.attempt_success is False for record in attempts)
    assert all(record.retryable is True for record in attempts)
    assert [getattr(record, "retry_delay_ms", None) for record in attempts] == [None]
    aggregates = [
        record for record in caplog.records
        if record.getMessage() == "Gemini provider call completed."
    ]
    assert len(aggregates) == 1
    assert aggregates[0].gemini_success is False


def test_timeout_logs_exactly_one_failed_attempt(caplog) -> None:
    client = MagicMock()
    client.models.generate_content.side_effect = httpx.ReadTimeout(
        "sensitive timeout"
    )

    with caplog.at_level(logging.INFO, logger="app.tools.gemini_client"):
        with pytest.raises(httpx.ReadTimeout):
            observed_generate_content(
                client,
                operation="claim_review",
                model="configured-model",
                location="global",
                claim_id="CLM-SAFE",
                contents="sensitive prompt",
            )

    client.models.generate_content.assert_called_once()
    attempts = attempt_records(caplog)
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].max_attempts == 1
    assert attempts[0].attempt_success is False
    assert attempts[0].exception_type == "ReadTimeout"
    assert attempts[0].retryable is True
    assert attempts[0].retry_delay_ms is None


def test_non_retryable_failure_logs_one_attempt(caplog) -> None:
    client = retrying_client(ProviderError("secret failure", 400))

    with caplog.at_level(logging.INFO, logger="app.tools.gemini_client"):
        with pytest.raises(ProviderError):
            observed_generate_content(
                client,
                operation="document_extraction",
                model="configured-model",
                location="global",
            )

    attempts = attempt_records(caplog)
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].attempt_success is False
    assert attempts[0].provider_status_code == 400
    assert attempts[0].exception_type == "ProviderError"
    assert attempts[0].retryable is False
    assert attempts[0].retry_delay_ms is None
