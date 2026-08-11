import re
from dataclasses import dataclass
from datetime import timezone
from typing import BinaryIO
from uuid import uuid4

from app.events.claim_events import (
    ClaimDocumentReceivedEvent,
    ClaimSubmittedEvent,
    DocumentReceivedPayload,
)
from app.domain.claimant_action_display import build_claimant_action_display
from app.domain.claimant_evidence_requests import build_claimant_evidence_requests
from app.events.pubsub_publisher import ClaimEventPublisher
from app.models.claim_api import (
    ClaimAcceptedResponse,
    ClaimSummaryResponse,
    ClaimTimelineEvent,
    DocumentAcceptedResponse,
)
from app.models.claim_document import ClaimDocument
from app.models.requested_action import (
    UploadDocumentRequestedAction,
    parse_requested_actions,
)
from app.services.claim_storage_service import (
    ClaimStorageService,
    ClaimStorageValidationError,
    ValidatedUpload,
    infer_document_type,
)
from app.services.document_extraction_service import SUPPORTED_RESUME_DOCUMENT_TYPES
from app.tools.firestore_repository import (
    FirestoreClaimRepository,
    generate_claim_id,
    generate_document_id,
    utc_now,
)


class ClaimSubmissionError(RuntimeError):
    def __init__(self, message: str, *, claim_id: str | None = None) -> None:
        super().__init__(message)
        self.claim_id = claim_id


class ClaimNotFoundError(LookupError):
    """Raised when a claimant API lookup targets an unknown claim."""


@dataclass
class EvidenceUpload:
    file_obj: BinaryIO
    filename: str | None
    content_type: str | None
    document_type: str | None = None


@dataclass
class PreparedEvidence:
    source: EvidenceUpload
    upload: ValidatedUpload
    document_type: str


class ClaimSubmissionService:
    def __init__(
        self,
        *,
        repository: FirestoreClaimRepository,
        storage_service: ClaimStorageService,
        publisher: ClaimEventPublisher,
    ) -> None:
        self._repository = repository
        self._storage = storage_service
        self._publisher = publisher

    def submit_claim(
        self,
        *,
        incident_description: str,
        policy_number_hint: str | None,
        evidence: list[EvidenceUpload],
        idempotency_key: str,
    ) -> ClaimAcceptedResponse:
        description = incident_description.strip()
        prepared = self._prepare_evidence(evidence)
        has_police_report = any(
            item.upload.content_type == "application/pdf"
            for item in prepared
        )
        if not description and not has_police_report:
            raise ClaimStorageValidationError(
                "Provide either an incident description or a police report."
            )
        if not any(item.upload.content_type.startswith("image/") for item in prepared):
            raise ClaimStorageValidationError(
                "At least one JPEG or PNG damage photo is required."
            )
        _validate_idempotency_key(idempotency_key)

        claim_id = generate_claim_id()
        event = ClaimSubmittedEvent(
            event_type="claim.submitted",
            claim_id=claim_id,
            source="claimant-api",
        )
        reservation = self._repository.create_idempotent_claim_shell(
            claim_id,
            idempotency_key=idempotency_key,
            incident_description=description,
            policy_number_hint=(policy_number_hint or "").strip() or None,
            submission_event_id=event.event_id,
            correlation_id=event.correlation_id,
        )
        if not reservation.created:
            return ClaimAcceptedResponse(
                claim_id=reservation.claim_id,
                status="new",
                event_id=reservation.event_id,
                message="Claim received and processing started.",
            )

        try:
            for item in prepared:
                self._store_document(claim_id, item)
        except Exception as exc:
            self._record_upload_failure(claim_id, event, exc)
            self._mark_idempotency_failed(idempotency_key, exc)
            raise ClaimSubmissionError(
                f"Claim {claim_id} was saved, but evidence upload failed.",
                claim_id=claim_id,
            ) from exc

        try:
            message_id = self._publisher.publish(event)
        except Exception as exc:
            self._repository.mark_claim_submission_publish_failed(
                claim_id,
                event_id=event.event_id,
                correlation_id=event.correlation_id,
                error_message=_safe_error(exc),
            )
            self._mark_idempotency_failed(idempotency_key, exc)
            raise ClaimSubmissionError(
                f"Claim {claim_id} was saved, but processing could not be started.",
                claim_id=claim_id,
            ) from exc

        self._repository.mark_claim_submission_published(
            claim_id,
            event_id=event.event_id,
            pubsub_message_id=message_id,
            correlation_id=event.correlation_id,
        )
        self._repository.mark_claim_submission_idempotency(
            idempotency_key,
            status="completed",
            pubsub_message_id=message_id,
        )
        return ClaimAcceptedResponse(
            claim_id=claim_id,
            status="new",
            event_id=event.event_id,
            message="Claim received and processing started.",
        )

    def add_missing_document(
        self,
        *,
        claim_id: str,
        document_type: str,
        evidence: EvidenceUpload,
        requested_action_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> DocumentAcceptedResponse:
        if self._repository.get_claim(claim_id) is None:
            raise ClaimNotFoundError(f"Claim {claim_id} does not exist.")
        reservation = None
        replaces_document_id = None
        if requested_action_id:
            if not idempotency_key:
                raise ClaimStorageValidationError(
                    "X-Idempotency-Key is required for replacement evidence."
                )
            _validate_idempotency_key(idempotency_key)
            try:
                reservation = self._repository.reserve_replacement_upload(
                    claim_id=claim_id,
                    action_id=requested_action_id,
                    idempotency_key=idempotency_key,
                )
            except Exception as exc:
                raise ClaimStorageValidationError(str(exc)) from exc
            document_type = reservation.action.document_type
            replaces_document_id = reservation.action.replaces_document_id
            if not reservation.should_upload:
                if reservation.status == "stored":
                    event = ClaimDocumentReceivedEvent(
                        event_id=reservation.event_id,
                        event_type="claim.document.received",
                        claim_id=claim_id,
                        correlation_id=reservation.correlation_id,
                        source="claimant-api",
                        payload=DocumentReceivedPayload(
                            document_id=reservation.document_id
                        ),
                    )
                    self._publisher.publish(event)
                    self._repository.update_replacement_upload_status(
                        claim_id=claim_id,
                        action_id=requested_action_id,
                        document_id=reservation.document_id,
                        status="published",
                    )
                return DocumentAcceptedResponse(
                    claim_id=claim_id,
                    document_id=reservation.document_id,
                    status=(
                        "received"
                        if reservation.status in {"stored", "published"}
                        else "processing"
                    ),
                    event_id=reservation.event_id,
                )
        else:
            _validate_resume_document_type(document_type)
        prepared = self._prepare_evidence(
            [
                EvidenceUpload(
                    file_obj=evidence.file_obj,
                    filename=evidence.filename,
                    content_type=evidence.content_type,
                    document_type=document_type,
                )
            ]
        )[0]
        document_id = (
            reservation.document_id if reservation else generate_document_id()
        )
        event = ClaimDocumentReceivedEvent(
            event_id=(reservation.event_id if reservation else str(uuid4())),
            event_type="claim.document.received",
            claim_id=claim_id,
            correlation_id=(
                reservation.correlation_id if reservation else str(uuid4())
            ),
            source="claimant-api",
            payload=DocumentReceivedPayload(document_id=document_id),
        )
        try:
            self._store_document(
                claim_id,
                prepared,
                document_id=document_id,
                requested_action_id=requested_action_id,
                replaces_document_id=replaces_document_id,
            )
            if reservation:
                self._repository.update_replacement_upload_status(
                    claim_id=claim_id,
                    action_id=requested_action_id,
                    document_id=document_id,
                    status="stored",
                )
        except Exception as exc:
            if reservation:
                self._repository.update_replacement_upload_status(
                    claim_id=claim_id,
                    action_id=requested_action_id,
                    document_id=document_id,
                    status="retry_required",
                )
            self._repository.append_claim_event(
                claim_id,
                action="claim_document_upload_failed",
                actor="claimant_api",
                from_status=None,
                to_status=None,
                details={
                    "event_id": event.event_id,
                    "document_id": document_id,
                    "error": _safe_error(exc),
                },
                correlation_id=event.correlation_id,
                document_id=document_id,
                event_id=f"{event.event_id}-upload-failed",
            )
            raise ClaimSubmissionError(
                f"Document {document_id} could not be uploaded.", claim_id=claim_id
            ) from exc
        try:
            self._publisher.publish(event)
            if reservation:
                self._repository.update_replacement_upload_status(
                    claim_id=claim_id,
                    action_id=requested_action_id,
                    document_id=document_id,
                    status="published",
                )
        except Exception as exc:
            self._repository.append_claim_event(
                claim_id,
                action="claim_document_event_publish_failed",
                actor="claimant_api",
                from_status=None,
                to_status=None,
                details={
                    "event_id": event.event_id,
                    "document_id": document_id,
                    "error": _safe_error(exc),
                },
                correlation_id=event.correlation_id,
                document_id=document_id,
                event_id=f"{event.event_id}-publish-failed",
            )
            raise ClaimSubmissionError(
                f"Document {document_id} was saved, but resume could not be started.",
                claim_id=claim_id,
            ) from exc
        return DocumentAcceptedResponse(
            claim_id=claim_id,
            document_id=document_id,
            status="received",
            event_id=event.event_id,
        )

    def get_claim(self, claim_id: str) -> ClaimSummaryResponse:
        claim = self._repository.get_claim(claim_id)
        if claim is None:
            raise ClaimNotFoundError(f"Claim {claim_id} does not exist.")
        appointment = self._repository.get_scheduled_appointment(claim_id)
        updated_at = claim.get("updated_at") or claim.get("created_at")
        if updated_at is None:
            raise ClaimSubmissionError(
                f"Claim {claim_id} has no persisted timestamp.", claim_id=claim_id
            )
        requested_actions = parse_requested_actions(
            claim.get("requested_actions", [])
        )
        requested_evidence = build_claimant_evidence_requests(
            claim.get("missing_documents", []),
            claim.get("unusable_evidence", []),
        )
        if any(
            isinstance(action, UploadDocumentRequestedAction)
            for action in requested_actions
        ):
            requested_evidence = []
        return ClaimSummaryResponse(
            claim_id=claim_id,
            status=str(claim.get("status")),
            intake_priority=claim.get("intake_priority"),
            missing_documents=list(claim.get("missing_documents", [])),
            requested_evidence=requested_evidence,
            requested_actions=requested_actions,
            action_display=build_claimant_action_display(claim, requested_actions),
            inspection=(
                appointment.model_dump(mode="python") if appointment else None
            ),
            updated_at=updated_at,
        )

    def get_timeline(self, claim_id: str) -> list[ClaimTimelineEvent]:
        if self._repository.get_claim(claim_id) is None:
            raise ClaimNotFoundError(f"Claim {claim_id} does not exist.")
        events = sorted(
            self._repository.get_claim_events(claim_id),
            key=lambda item: item["timestamp"],
        )
        return [
            ClaimTimelineEvent(
                timestamp=item["timestamp"].astimezone(timezone.utc),
                action=str(item.get("action", "")),
                actor=str(item.get("actor", "")),
                from_status=item.get("from_status"),
                to_status=item.get("to_status"),
                details=dict(item.get("details") or {}),
                correlation_id=item.get("correlation_id"),
            )
            for item in events
        ]

    def _prepare_evidence(
        self, evidence: list[EvidenceUpload]
    ) -> list[PreparedEvidence]:
        if not evidence:
            raise ClaimStorageValidationError("At least one evidence file is required.")
        if len(evidence) > 10:
            raise ClaimStorageValidationError(
                "A claim submission supports at most 10 evidence files."
            )
        prepared = []
        for item in evidence:
            upload = self._storage.validate_upload(
                item.file_obj,
                filename=item.filename,
                content_type=item.content_type,
            )
            prepared.append(
                PreparedEvidence(
                    source=item,
                    upload=upload,
                    document_type=item.document_type
                    or infer_document_type(upload.content_type),
                )
            )
        return prepared

    def _store_document(
        self,
        claim_id: str,
        item: PreparedEvidence,
        *,
        document_id: str | None = None,
        requested_action_id: str | None = None,
        replaces_document_id: str | None = None,
    ) -> str:
        document_id = document_id or generate_document_id()
        stored = self._storage.upload_claim_document(
            claim_id=claim_id,
            document_id=document_id,
            file_obj=item.source.file_obj,
            upload=item.upload,
        )
        self._repository.add_document(
            ClaimDocument(
                document_id=document_id,
                claim_id=claim_id,
                document_type=item.document_type,
                requested_action_id=requested_action_id,
                replaces_document_id=replaces_document_id,
                filename=stored.filename,
                content_type=stored.content_type,
                storage_path=stored.gs_uri,
                gs_uri=stored.gs_uri,
                bucket=stored.bucket,
                object_name=stored.object_name,
                size_bytes=stored.size_bytes,
                received_at=utc_now(),
            )
        )
        return document_id

    def _record_upload_failure(
        self,
        claim_id: str,
        event: ClaimSubmittedEvent,
        exc: Exception,
    ) -> None:
        self._repository.mark_claim_submission_upload_failed(
            claim_id,
            event_id=event.event_id,
            correlation_id=event.correlation_id,
            error_message=_safe_error(exc),
        )

    def _mark_idempotency_failed(
        self, idempotency_key: str, exc: Exception
    ) -> None:
        self._repository.mark_claim_submission_idempotency(
            idempotency_key,
            status="failed",
            error_message=_safe_error(exc),
        )


def _validate_resume_document_type(document_type: str) -> None:
    if not re.fullmatch(r"[a-z0-9_]{1,64}", document_type):
        raise ClaimStorageValidationError("Invalid document_type.")
    if (
        document_type not in SUPPORTED_RESUME_DOCUMENT_TYPES
        and not document_type.startswith("police_report_page_")
    ):
        raise ClaimStorageValidationError(
            f"Unsupported missing-document type: {document_type}"
        )


def _validate_idempotency_key(idempotency_key: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key):
        raise ClaimStorageValidationError(
            "X-Idempotency-Key must be 8-128 URL-safe characters."
        )


def _safe_error(exc: Exception) -> str:
    message = re.sub(r"[\r\n]+", " ", str(exc)).strip()[:500]
    message = re.sub(
        r"(?i)(authorization|token|secret|signature)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        message,
    )
    return message or type(exc).__name__
