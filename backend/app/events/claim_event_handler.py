import re
from typing import Any, Protocol

from pydantic import BaseModel

from app.agents.firstnotice_adk import UnsupportedCoordinatorState
from app.events.claim_events import (
    ClaimCorrectionReceivedEvent,
    ClaimDocumentReceivedEvent,
    ClaimEvent,
    ClaimHumanReviewApprovedEvent,
    ClaimHumanReviewCorrectionRequestedEvent,
    ClaimHumanReviewManualHandlingEvent,
    ClaimInspectionReadyEvent,
    ClaimSubmittedEvent,
    inspection_ready_event_id,
)
from app.events.pubsub_publisher import ClaimEventPublisher
from app.integrations.google_calendar_service import GoogleCalendarError
from app.integrations.gmail_service import GmailError
from app.services.document_extraction_service import (
    UnsupportedResumeDocumentTypeError,
)
from app.tools.firestore_repository import (
    FirestoreClaimRepository,
    FirestoreReadError,
    FirestoreWriteError,
)
from app.workflows.claim_dispatch_workflow import ClaimDispatchWorkflowError
from app.workflows.claim_resume_workflow import ClaimResumeError


class SubmittedClaimCoordinator(Protocol):
    async def process_submitted_claim(self, claim_id: str) -> dict[str, Any]: ...


class EventHandlingResult(BaseModel):
    event_id: str
    event_type: str
    claim_id: str
    outcome: str
    claim_status: str | None = None
    duplicate: bool = False


class NonRetryableEventError(RuntimeError):
    """Raised when redelivery cannot make an invalid event processable."""


class ClaimEventProcessingError(RuntimeError):
    def __init__(
        self, message: str, *, retryable: bool, stage: str = "unknown"
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.stage = stage


class ClaimEventHandler:
    """Validate idempotency and route events without owning business logic."""

    def __init__(
        self,
        *,
        repository: FirestoreClaimRepository,
        coordinator: SubmittedClaimCoordinator,
        resume_workflow: Any,
        dispatch_workflow: Any,
        publisher: ClaimEventPublisher,
        human_review_service: Any | None = None,
        human_review_resume_workflow: Any | None = None,
    ) -> None:
        self._repository = repository
        self._coordinator = coordinator
        self._resume_workflow = resume_workflow
        self._dispatch_workflow = dispatch_workflow
        self._publisher = publisher
        self._human_review_service = human_review_service
        self._human_review_resume_workflow = human_review_resume_workflow

    async def handle(self, event: ClaimEvent) -> EventHandlingResult:
        try:
            should_process = self._repository.begin_claim_event(
                event.claim_id,
                event_id=event.event_id,
                event_type=event.event_type,
                event_version=event.event_version,
                occurred_at=event.occurred_at,
                correlation_id=event.correlation_id,
                source=event.source,
            )
        except Exception as exc:
            raise ClaimEventProcessingError(
                "Could not reserve the event idempotency record.",
                retryable=True,
                stage="event_reservation",
            ) from exc

        if not should_process:
            try:
                status, _ = self._ensure_inspection_dispatch_boundary(event)
                self._ensure_human_review_boundary(event, status)
            except Exception as exc:
                raise ClaimEventProcessingError(
                    "Could not reconcile a durable workflow boundary.",
                    retryable=True,
                    stage="boundary_reconciliation",
                ) from exc
            return EventHandlingResult(
                event_id=event.event_id,
                event_type=event.event_type,
                claim_id=event.claim_id,
                outcome="duplicate_no_op",
                claim_status=status,
                duplicate=True,
            )

        try:
            stage = "business_event_route"
            route_result = await self._route(event)
            stage = "inspection_dispatch_boundary"
            status, inspection_ready_message_id = (
                self._ensure_inspection_dispatch_boundary(event)
            )
            stage = "inspection_decision_boundary"
            review_request = self._ensure_human_review_boundary(event, status)
            if review_request is not None:
                route_result = {**route_result, "human_review": review_request}
            if inspection_ready_message_id is not None:
                route_result = {
                    **route_result,
                    "inspection_ready_message_id": inspection_ready_message_id,
                }
            result = EventHandlingResult(
                event_id=event.event_id,
                event_type=event.event_type,
                claim_id=event.claim_id,
                outcome="processed",
                claim_status=status,
            )
            stage = "event_completion"
            self._repository.complete_claim_event(
                event.claim_id,
                event_id=event.event_id,
                event_type=event.event_type,
                correlation_id=event.correlation_id,
                result={"claim_status": status, "route_result": route_result},
            )
            return result
        except Exception as exc:
            retryable = _is_retryable(exc)
            safe_message = _safe_error_message(exc)
            try:
                self._repository.fail_claim_event(
                    event.claim_id,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    correlation_id=event.correlation_id,
                    error_type=type(exc).__name__,
                    error_message=safe_message,
                    retryable=retryable,
                )
            except Exception as persistence_exc:
                raise ClaimEventProcessingError(
                    "Event processing and failure recording both failed.",
                    retryable=True,
                    stage="failure_recording",
                ) from persistence_exc
            raise ClaimEventProcessingError(
                f"Event {event.event_id} failed: {safe_message}",
                retryable=retryable,
                stage=stage,
            ) from exc

    async def _route(self, event: ClaimEvent) -> dict[str, Any]:
        if isinstance(event, ClaimSubmittedEvent):
            claim = self._repository.get_claim(event.claim_id)
            if claim is None:
                raise NonRetryableEventError(
                    f"Claim {event.claim_id} does not exist."
                )
            status = str(claim.get("status", ""))
            if status in {
                "inspection_ready", "inspection_pending", "inspection_scheduled"
            }:
                return {"action": "already_ready_for_inspection", "status": status}
            return await self._coordinator.process_submitted_claim(event.claim_id)

        if isinstance(event, ClaimDocumentReceivedEvent):
            document = self._repository.get_document(
                event.claim_id, event.payload.document_id
            )
            if document is None:
                raise NonRetryableEventError(
                    f"Document {event.payload.document_id} does not exist under "
                    f"claim {event.claim_id}."
                )
            result = self._resume_workflow.resume(event.claim_id, document)
            return result.model_dump(mode="python")

        if isinstance(event, ClaimInspectionReadyEvent):
            result = self._dispatch_workflow.dispatch(event.claim_id)
            return result.model_dump(mode="python")

        if isinstance(event, ClaimHumanReviewApprovedEvent):
            if self._human_review_resume_workflow is None:
                raise NonRetryableEventError("Human-review resume is not configured.")
            return self._human_review_resume_workflow.resume_approved(
                event.claim_id, event.payload.review_id, event.correlation_id
            )

        if isinstance(event, ClaimHumanReviewCorrectionRequestedEvent):
            if self._human_review_resume_workflow is None:
                raise NonRetryableEventError("Human-review resume is not configured.")
            return self._human_review_resume_workflow.request_correction(
                event.claim_id, event.payload.review_id, event.correlation_id
            )

        if isinstance(event, ClaimHumanReviewManualHandlingEvent):
            if self._human_review_resume_workflow is None:
                raise NonRetryableEventError("Human-review resume is not configured.")
            return self._human_review_resume_workflow.continue_manual_handling(
                event.claim_id, event.payload.review_id, event.correlation_id
            )

        if isinstance(event, ClaimCorrectionReceivedEvent):
            if self._human_review_resume_workflow is None:
                raise NonRetryableEventError("Human-review resume is not configured.")
            return self._human_review_resume_workflow.resume_correction(
                event.claim_id,
                event.payload.review_id,
                event.payload.field_name,
                event.correlation_id,
            )

        raise NonRetryableEventError(f"Unsupported event type: {event.event_type}")

    def _ensure_human_review_boundary(
        self, event: ClaimEvent, status: str | None
    ) -> dict[str, str] | None:
        if status not in {"inspection_ready", "human_review_required"} or self._human_review_service is None:
            return None
        review = self._human_review_service.ensure_review_requested(
            event.claim_id, correlation_id=event.correlation_id
        )
        return {
            "review_id": review.review_id,
            "notification_status": review.notification_status,
        }

    def _ensure_inspection_dispatch_boundary(
        self, event: ClaimEvent
    ) -> tuple[str | None, str | None]:
        """Reload durable state and publish the deterministic dispatch wake-up.

        This runs for both first deliveries and duplicate recovery so a process
        interruption after a workflow transition cannot strand an eligible claim.
        """
        status = self._claim_status(event.claim_id)
        if isinstance(event, ClaimInspectionReadyEvent):
            return status, None
        if status != "inspection_pending":
            return status, None
        ready_event = ClaimInspectionReadyEvent(
            event_id=inspection_ready_event_id(event.claim_id),
            event_type="claim.inspection.ready",
            claim_id=event.claim_id,
            correlation_id=event.correlation_id,
            source="claim-event-handler",
        )
        return status, self._publisher.publish(ready_event)

    def _claim_status(self, claim_id: str) -> str | None:
        claim = self._repository.get_claim(claim_id)
        return str(claim.get("status")) if claim else None


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, GmailError):
        return exc.retryable
    if isinstance(exc, GoogleCalendarError):
        return exc.retryable
    non_retryable = (
        NonRetryableEventError,
        UnsupportedCoordinatorState,
        ClaimResumeError,
        ClaimDispatchWorkflowError,
        UnsupportedResumeDocumentTypeError,
        ValueError,
    )
    if isinstance(exc, non_retryable):
        return False
    if isinstance(exc, (FirestoreReadError, FirestoreWriteError)):
        return True
    return True


def _safe_error_message(exc: Exception) -> str:
    message = re.sub(r"[\r\n]+", " ", str(exc)).strip()[:500]
    message = re.sub(
        r"(?i)(api[_-]?key|authorization|token|secret)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        message,
    )
    return message or "Event processing failed."
