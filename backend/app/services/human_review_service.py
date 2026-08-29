import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import BinaryIO
from urllib.parse import quote

from app.domain.claim_status import ClaimStatus
from app.events.claim_events import (
    ClaimCorrectionReceivedEvent,
    ClaimDocumentReceivedEvent,
    ClaimHumanReviewApprovedEvent,
    ClaimHumanReviewCorrectionRequestedEvent,
    ClaimHumanReviewManualHandlingEvent,
    CorrectionReceivedPayload,
    DocumentReceivedPayload,
    HumanReviewPayload,
    human_review_event_id,
)
from app.events.pubsub_publisher import ClaimEventPublisher
from app.integrations.gmail_service import (
    GmailError,
    GmailSender,
    HumanReviewEmailRequest,
)
from app.models.human_review import (
    ClaimCorrectionAcceptedResponse,
    HumanReviewBriefing,
    HumanReviewDecisionRequest,
    HumanReviewDecisionResponse,
    HumanReviewPublicResponse,
    HumanReviewRecord,
    RecommendedRemediation,
    human_review_id,
)
from app.models.claim_document import ClaimDocument
from app.models.requested_action import (
    EnterTextRequestedAction,
    EvidenceSourceReference,
    UploadDocumentRequestedAction,
)
from app.services.document_extraction_service import SUPPORTED_RESUME_DOCUMENT_TYPES
from app.services.claim_storage_service import (
    ClaimStorageService,
    ClaimStorageValidationError,
)
from app.services.voice_incident_extraction_service import VoiceIncidentExtractor
from app.tools.firestore_repository import FirestoreClaimRepository


class HumanReviewError(RuntimeError):
    """Base error for the token-protected operational review checkpoint."""


class HumanReviewNotFoundError(HumanReviewError):
    pass


class HumanReviewExpiredError(HumanReviewError):
    pass


class HumanReviewConflictError(HumanReviewError):
    pass


@dataclass(frozen=True)
class HumanReviewSettings:
    web_base_url: str
    token_ttl_minutes: int = 60

    @classmethod
    def from_env(cls) -> "HumanReviewSettings":
        base_url = os.getenv(
            "FIRSTNOTICE_WEB_BASE_URL", "http://localhost:4200"
        ).strip().rstrip("/")
        try:
            ttl = int(os.getenv("HUMAN_REVIEW_TOKEN_TTL_MINUTES", "60"))
        except ValueError as exc:
            raise HumanReviewError(
                "HUMAN_REVIEW_TOKEN_TTL_MINUTES must be an integer."
            ) from exc
        if not base_url.startswith(("http://", "https://")):
            raise HumanReviewError("FIRSTNOTICE_WEB_BASE_URL must be an HTTP(S) URL.")
        if not 5 <= ttl <= 1440:
            raise HumanReviewError(
                "HUMAN_REVIEW_TOKEN_TTL_MINUTES must be between 5 and 1440."
            )
        return cls(base_url, ttl)


def hash_review_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_incident_date(value: str) -> date:
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError as exc:
        raise HumanReviewConflictError(
            "Please provide a valid incident date in YYYY-MM-DD format."
        ) from exc
    if parsed_date.isoformat() != value or parsed_date > datetime.now(
        timezone.utc
    ).date():
        raise HumanReviewConflictError(
            "Please provide a valid incident date that is not in the future."
        )
    return parsed_date


def _voice_incident_requirements(
    claim: dict[str, object], action_field: str
) -> tuple[bool, bool]:
    missing = {
        str(item.get("type"))
        for item in claim.get("missing_documents", [])
        if isinstance(item, dict)
        and item.get("type") in {"incident_date", "incident_description"}
    }
    return (
        action_field in {"incident_date", "incident_information"}
        or "incident_date" in missing,
        action_field in {"incident_description", "incident_information"}
        or "incident_description" in missing,
    )


class HumanReviewService:
    def __init__(
        self,
        *,
        repository: FirestoreClaimRepository,
        publisher: ClaimEventPublisher,
        settings: HumanReviewSettings,
        gmail_sender: GmailSender | None = None,
        storage_service: ClaimStorageService | None = None,
        voice_incident_extractor: VoiceIncidentExtractor | None = None,
        recipient: str = "",
        sender: str = "",
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._settings = settings
        self._gmail_sender = gmail_sender
        self._storage_service = storage_service
        self._voice_incident_extractor = voice_incident_extractor
        self._recipient = recipient
        self._sender = sender

    def ensure_review_requested(
        self, claim_id: str, *, correlation_id: str
    ) -> HumanReviewRecord:
        claim = self._repository.get_claim(claim_id)
        status = claim.get("status") if claim else None
        if status not in {
            ClaimStatus.INSPECTION_READY,
            ClaimStatus.HUMAN_REVIEW_REQUIRED,
        }:
            raise HumanReviewConflictError(
                "An inspection decision can only be requested for inspection_ready."
            )
        generation, generation_key, review_id = self._current_review_generation(
            claim_id, claim
        )
        existing = self._repository.get_human_review(claim_id, review_id)
        token: str | None = None
        now = datetime.now(timezone.utc)
        if existing is None:
            token = secrets.token_urlsafe(32)
            source_references = _conflict_source_references(
                claim, self._repository.get_documents(claim_id)
            )
            remediation, remediation_target = _recommended_remediation(
                claim, source_references
            )
            review = HumanReviewRecord(
                review_id=review_id,
                claim_id=claim_id,
                status="pending",
                reason=(
                    "Autonomous intake is complete and ready for an inspection decision."
                    if status == ClaimStatus.INSPECTION_READY
                    else str(
                        claim.get("human_review_reason")
                        or "Human verification is required."
                    )
                ),
                briefing=_briefing_from_claim(claim),
                conflict_fields=[
                    str(item.get("field"))
                    for item in claim.get("conflicts", [])
                    if isinstance(item, dict) and item.get("field")
                ],
                source_references=source_references,
                generation=generation,
                generation_key=generation_key,
                conflicts=[
                    item
                    for item in claim.get("conflicts", [])
                    if isinstance(item, dict)
                    and item.get("values")
                    and item.get("reason")
                ],
                unresolved_uncertainties=[
                    item
                    for item in claim.get("unresolved_uncertainties", [])
                    if isinstance(item, dict)
                ],
                issue_fingerprints=_current_issue_fingerprints(claim),
                recommended_remediation=remediation,
                recommended_target_document_id=remediation_target,
                token_hash=hash_review_token(token),
                created_at=now,
                expires_at=now + timedelta(minutes=self._settings.token_ttl_minutes),
                correlation_id=correlation_id,
            )
            created = self._repository.create_human_review(review)
            if not created:
                review = self._repository.get_human_review(claim_id, review_id)
                if review is None:
                    raise HumanReviewError("Could not load the reserved review checkpoint.")
                token = None
        else:
            review = existing

        if review.notification_status == "sent":
            return review
        if self._gmail_sender is None:
            if review.notification_status != "disabled":
                self._repository.mark_human_review_notification(
                    claim_id,
                    review_id,
                    status="disabled",
                    correlation_id=correlation_id,
                    review_generation=generation,
                )
            return review.model_copy(update={"notification_status": "disabled"})

        if token is None:
            token = secrets.token_urlsafe(32)
            new_hash = hash_review_token(token)
            expires_at = now + timedelta(minutes=self._settings.token_ttl_minutes)
            self._repository.replace_human_review_token(
                review,
                old_token_hash=review.token_hash,
                new_token_hash=new_hash,
                expires_at=expires_at,
            )
            review = review.model_copy(
                update={
                    "token_hash": new_hash,
                    "expires_at": expires_at,
                    "notification_status": "pending",
                }
            )
        request = HumanReviewEmailRequest(
            notification_id=f"{review.review_id}-REQUEST",
            claim_id=claim_id,
            recipient=self._recipient,
            sender=self._sender,
            subject=(
                f"Inspection Decision Ready - {claim_id}"
                if status == ClaimStatus.INSPECTION_READY
                else f"Human Review Required - {claim_id}"
            ),
            reason=review.reason,
            summary=review.briefing.summary,
            conflicts=review.briefing.conflicts,
            unresolved_questions=review.briefing.unresolved_questions,
            recommended_next_action=review.briefing.recommended_next_action,
            review_url=(
                f"{self._settings.web_base_url}/#/adjuster/review/{quote(token, safe='')}"
            ),
        )
        try:
            sent = self._gmail_sender.send_human_review_email(request)
        except GmailError:
            self._repository.mark_human_review_notification(
                claim_id,
                review_id,
                status="failed",
                correlation_id=correlation_id,
                review_generation=generation,
            )
            raise
        self._repository.mark_human_review_notification(
            claim_id,
            review_id,
            status="sent",
            gmail_message_id=sent.gmail_message_id,
            correlation_id=correlation_id,
            review_generation=generation,
        )
        return review.model_copy(
            update={"notification_status": "sent", "gmail_message_id": sent.gmail_message_id}
        )

    def _current_review_generation(
        self, claim_id: str, claim: dict[str, object]
    ) -> tuple[int, str, str]:
        current_id = str(claim.get("current_human_review_id") or "").strip()
        current_key = str(
            claim.get("current_human_review_generation_key") or ""
        ).strip()
        current_generation = int(
            claim.get("current_human_review_generation") or 0
        )
        if current_id and current_key and current_generation > 0:
            return current_generation, current_key, current_id

        cycle_one_id = human_review_id(claim_id, 1)
        cycle_one = self._repository.get_human_review(claim_id, cycle_one_id)
        reentry_key = (
            _legacy_reentry_generation_key(
                claim_id,
                cycle_one.review_id,
                self._repository.get_claim_events(claim_id),
            )
            if cycle_one is not None and cycle_one.status != "pending"
            else None
        )
        if reentry_key:
            reserved = self._repository.reserve_human_review_generation(
                claim_id=claim_id,
                generation_key=reentry_key,
                floor_generation=1,
                make_current=True,
            )
            return (
                reserved.generation,
                reserved.generation_key,
                reserved.review_id,
            )

        generation_key = (
            cycle_one.generation_key
            if cycle_one is not None
            else f"{claim_id}:submitted-review:v1"
        )
        self._repository.set_current_human_review_generation(
            claim_id=claim_id,
            generation=1,
            generation_key=generation_key,
            review_id=cycle_one_id,
        )
        return 1, generation_key, cycle_one_id

    def get_public_review(self, token: str) -> HumanReviewPublicResponse:
        review = self._review_for_token(token)
        claim = self._repository.get_claim(review.claim_id) or {}
        documents = [
            item
            for item in self._repository.get_documents(review.claim_id)
            if item.status != "superseded"
        ]
        active_document_ids = {item.document_id for item in documents}
        document_types_by_source: dict[str, set[str]] = {}
        for document in documents:
            document_types_by_source.setdefault(
                document.filename.strip().casefold(), set()
            ).add(document.document_type)
        return HumanReviewPublicResponse.model_validate({
            **review.model_dump(),
            "source_references": [
                item.model_dump(mode="python")
                for item in review.source_references
                if item.document_id in active_document_ids
            ],
            "supporting_documents": [
                {
                    "document_id": item.document_id,
                    "filename": item.filename,
                    "document_type": item.document_type,
                    "status": item.status,
                }
                for item in documents
                if item.document_type == "medical_document"
                and item.status != "superseded"
            ],
            "claimant_voice_updates": [
                _claimant_voice_update(item, claim, review)
                for item in documents
                if item.source_type == "claimant_voice"
            ],
            "checkpoint_status": claim.get("status"),
            "ai_recommendation": _inspection_recommendation(claim),
            "claim_snapshot": _inspection_claim_snapshot(claim, documents),
            "evidence_comparison": [
                {
                    "source": str(item.get("source")),
                    "finding": str(item.get("finding")),
                    **_comparison_document_type(
                        str(item.get("source")), document_types_by_source
                    ),
                }
                for item in claim.get("current_evidence_findings", [])
                if isinstance(item, dict)
                and item.get("source")
                and item.get("finding")
            ],
            "resolution_history": _resolution_history(
                self._repository.get_claim_events(review.claim_id)
            ),
        })

    def get_supporting_document(
        self, token: str, document_id: str
    ) -> tuple[bytes, str, str]:
        review = self._review_for_token(token)
        document = self._repository.get_document(review.claim_id, document_id)
        if (
            document is None
            or document.status == "superseded"
            or document.document_type != "medical_document"
            or not document.object_name
            or self._storage_service is None
        ):
            raise HumanReviewNotFoundError("Supporting document is unavailable.")
        return (
            self._storage_service.download_claim_document(document.object_name),
            document.filename,
            document.content_type or "application/octet-stream",
        )

    def approve(
        self, token: str, request: HumanReviewDecisionRequest
    ) -> HumanReviewDecisionResponse:
        candidate = self._repository.get_human_review_by_token_hash(
            hash_review_token(token)
        )
        if isinstance(candidate, HumanReviewRecord):
            self._ensure_current_decision(candidate)
        return self._decide(token, "approved", request)

    def request_correction(
        self, token: str, request: HumanReviewDecisionRequest
    ) -> HumanReviewDecisionResponse:
        review = self._review_for_token(token)
        self._ensure_current_decision(review)
        claim = self._repository.get_claim(review.claim_id)
        if request.requested_evidence:
            unsupported = [
                item.document_type
                for item in request.requested_evidence
                if item.document_type not in SUPPORTED_RESUME_DOCUMENT_TYPES
                and not item.document_type.startswith("police_report_page_")
            ]
            if unsupported:
                raise HumanReviewConflictError(
                    f"Unsupported requested evidence type: {unsupported[0]}"
                )
            for item in request.requested_evidence:
                if not item.replaces_document_id:
                    continue
                target = self._repository.get_document(
                    review.claim_id, item.replaces_document_id
                )
                if (
                    target is None
                    or target.claim_id != review.claim_id
                    or target.status == "superseded"
                    or target.document_type != item.document_type
                ):
                    raise HumanReviewConflictError(
                        "A requested replacement target is not an active matching "
                        "document for this claim."
                    )
            authoritative = request.model_copy(
                update={
                    "correction_type": "upload_document",
                    "target_document_id": None,
                    "decision_note": request.decision_note
                    or "The adjuster requested additional evidence.",
                }
            )
            return self._decide(token, "correction_requested", authoritative)
        instruction = (request.decision_note or "").strip()
        action_type, _ = _interpret_adjuster_instruction(instruction)
        authoritative = request.model_copy(
            update={"correction_type": action_type, "target_document_id": None}
        )
        return self._decide(token, "correction_requested", authoritative)

    def continue_manual_handling(
        self, token: str, request: HumanReviewDecisionRequest
    ) -> HumanReviewDecisionResponse:
        review = self._review_for_token(token)
        self._ensure_current_decision(review)
        claim = self._repository.get_claim(review.claim_id)
        if claim is None or claim.get("status") != ClaimStatus.HUMAN_REVIEW_REQUIRED:
            raise HumanReviewConflictError(
                "Manual handling is available only for a claim requiring human review."
            )
        return self._decide(token, "manual_handling", request)

    def _ensure_current_decision(self, review: HumanReviewRecord) -> None:
        claim = self._repository.get_claim(review.claim_id)
        if (
            claim is None
            or claim.get("status") not in {
                ClaimStatus.INSPECTION_READY,
                ClaimStatus.HUMAN_REVIEW_REQUIRED,
            }
            or (
                claim.get("current_human_review_id")
                and claim.get("current_human_review_id") != review.review_id
            )
        ):
            raise HumanReviewConflictError(
                "This inspection decision link is no longer current."
            )

    def _decide(
        self,
        token: str,
        decision: str,
        request: HumanReviewDecisionRequest,
    ) -> HumanReviewDecisionResponse:
        token_hash = hash_review_token(token)
        review, duplicate = self._repository.decide_human_review(
            token_hash=token_hash,
            decision=decision,
            decision_note=request.decision_note,
            reviewer_label=request.reviewer_label,
            correction_type=(request.correction_type or "text"),
            target_document_id=request.target_document_id,
            requested_evidence=[
                item.model_dump(mode="python") for item in request.requested_evidence
            ],
            now=datetime.now(timezone.utc),
        )
        if review is None:
            raise HumanReviewNotFoundError("Review link is invalid.")
        if review.status == "expired":
            raise HumanReviewExpiredError("This review link has expired.")
        if duplicate and review.status not in {
            "approved", "correction_requested", "manual_handling"
        }:
            raise HumanReviewConflictError("This review cannot accept a decision.")
        if duplicate and review.status != decision:
            return _decision_response(review, duplicate=True)

        event_id = review.decision_event_id or human_review_event_id(
            review.claim_id, review.review_id, decision
        )
        if not duplicate or review.decision_publish_status != "published":
            event_cls = (
                ClaimHumanReviewApprovedEvent
                if decision == "approved"
                else ClaimHumanReviewCorrectionRequestedEvent
                if decision == "correction_requested"
                else ClaimHumanReviewManualHandlingEvent
            )
            event = event_cls(
                event_id=event_id,
                event_type=f"claim.human_review.{decision}",
                claim_id=review.claim_id,
                correlation_id=review.correlation_id,
                source="human-review-api",
                payload=HumanReviewPayload(review_id=review.review_id),
            )
            self._repository.append_claim_event(
                review.claim_id,
                action=(
                    "human_review_approved"
                    if decision == "approved"
                    else "human_review_correction_requested"
                    if decision == "correction_requested"
                    else "human_review_manual_handling_selected"
                ),
                actor="adjuster",
                from_status=str((self._repository.get_claim(review.claim_id) or {}).get("status")),
                to_status=str((self._repository.get_claim(review.claim_id) or {}).get("status")),
                details={
                    "review_id": review.review_id,
                    "review_generation": review.generation,
                },
                correlation_id=review.correlation_id,
                event_id=f"{event_id}-decision",
            )
            try:
                self._publisher.publish(event)
            except Exception:
                self._repository.mark_human_review_decision_published(
                    review.claim_id, review.review_id, published=False
                )
                raise
            self._repository.mark_human_review_decision_published(
                review.claim_id, review.review_id, published=True
            )
        return _decision_response(review, duplicate=duplicate)

    def submit_correction(
        self, claim_id: str, *, field_name: str, value: str
    ) -> ClaimCorrectionAcceptedResponse:
        claim = self._repository.get_claim(claim_id)
        if claim is None:
            raise HumanReviewNotFoundError(f"Claim {claim_id} does not exist.")
        actions = [
            item for item in claim.get("requested_actions", []) if isinstance(item, dict)
        ]
        action = next((item for item in actions if item.get("field_name") == field_name), None)
        if claim.get("status") != ClaimStatus.AWAITING_DOCUMENTS or action is None:
            raise HumanReviewConflictError("This correction is not currently requested.")
        value = value.strip()
        if field_name == "incident_date":
            _validate_incident_date(value)
        review_id = str(action["review_id"])
        event = ClaimCorrectionReceivedEvent(
            event_type="claim.correction.received",
            claim_id=claim_id,
            correlation_id=str(claim.get("correction_correlation_id") or review_id),
            source="claimant-api",
            payload=CorrectionReceivedPayload(
                review_id=review_id,
                field_name=field_name,
                source_type="claimant_manual",
            ),
        )
        self._repository.save_claim_correction(
            claim_id=claim_id,
            event_id=event.event_id,
            field_name=field_name,
            value=value,
            correlation_id=event.correlation_id,
        )
        self._publisher.publish(event)
        return ClaimCorrectionAcceptedResponse(claim_id=claim_id, event_id=event.event_id)

    def submit_voice_incident_correction(
        self,
        claim_id: str,
        *,
        requested_action_id: str,
        idempotency_key: str,
        file_obj: BinaryIO,
        filename: str | None,
        content_type: str | None,
    ) -> ClaimCorrectionAcceptedResponse:
        if self._storage_service is None:
            raise HumanReviewError("Voice incident remediation is not configured.")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key):
            raise HumanReviewConflictError(
                "X-Idempotency-Key must be 8-128 URL-safe characters."
            )
        claim = self._repository.get_claim(claim_id)
        if claim is None:
            raise HumanReviewNotFoundError(f"Claim {claim_id} does not exist.")
        action = next(
            (
                item
                for item in claim.get("requested_actions", [])
                if isinstance(item, dict)
                and item.get("action_type") == "enter_text"
                and item.get("field_name")
                in {"incident_date", "incident_description", "incident_information"}
                and item.get("action_id") == requested_action_id
            ),
            None,
        )
        if claim.get("status") != ClaimStatus.AWAITING_DOCUMENTS or action is None:
            raise HumanReviewConflictError(
                "Voice input is not currently requested for this claim."
            )

        digest = hashlib.sha256(
            f"{claim_id}:{requested_action_id}:{idempotency_key}".encode("utf-8")
        ).hexdigest()
        document_id = f"DOC-{digest[:8].upper()}"
        action_field = str(action["field_name"])
        needs_date, needs_context = _voice_incident_requirements(
            claim, action_field
        )
        for document in self._repository.get_documents(claim_id):
            if document.document_id == document_id or document.status == "superseded":
                continue
            if needs_date and document.evidence_facts.get("incident_date"):
                raise HumanReviewConflictError(
                    "Existing evidence already provides an incident date; voice cannot overwrite it."
                )
        if needs_date and claim.get("incident_date"):
            raise HumanReviewConflictError(
                "The claim already has an incident date; voice cannot overwrite it."
            )
        if needs_context and str(claim.get("incident_description") or "").strip():
            raise HumanReviewConflictError(
                "The claim already has claimant incident context; voice cannot overwrite it."
            )

        document = self._repository.get_document(claim_id, document_id)
        if document is None:
            try:
                upload = self._storage_service.validate_upload(
                    file_obj,
                    filename=filename or "incident-voice-note",
                    content_type=content_type,
                )
            except ClaimStorageValidationError as exc:
                raise HumanReviewConflictError(str(exc)) from exc
            if not upload.content_type.startswith("audio/"):
                raise HumanReviewConflictError("The voice response must be an audio file.")
            stored = self._storage_service.upload_claim_document(
                claim_id=claim_id,
                document_id=document_id,
                file_obj=file_obj,
                upload=upload,
            )
            document = ClaimDocument(
                document_id=document_id,
                claim_id=claim_id,
                document_type="voice_note",
                source_type="claimant_voice",
                requested_action_id=requested_action_id,
                filename=stored.filename,
                content_type=stored.content_type,
                storage_path=stored.gs_uri,
                gs_uri=stored.gs_uri,
                bucket=stored.bucket,
                object_name=stored.object_name,
                size_bytes=stored.size_bytes,
                received_at=datetime.now(timezone.utc),
            )
            self._repository.add_document(document)
        event = ClaimDocumentReceivedEvent(
            event_id=f"{claim_id}:{requested_action_id}:voice-upload:{digest[:16]}",
            event_type="claim.document.received",
            claim_id=claim_id,
            correlation_id=f"{requested_action_id}:{digest[16:32]}",
            source="claimant-api",
            payload=DocumentReceivedPayload(document_id=document_id),
        )
        self._repository.mark_voice_correction_processing(
            claim_id=claim_id,
            document_id=document_id,
            requested_action_id=requested_action_id,
            status="processing",
            correlation_id=event.correlation_id,
        )
        self._publisher.publish(event)
        return ClaimCorrectionAcceptedResponse(claim_id=claim_id, event_id=event.event_id)

    def process_voice_incident_document(
        self, claim_id: str, document_id: str
    ) -> dict[str, str]:
        if self._voice_incident_extractor is None:
            raise HumanReviewError("Voice incident extraction is not configured.")
        claim = self._repository.get_claim(claim_id)
        document = self._repository.get_document(claim_id, document_id)
        if claim is None or document is None:
            raise HumanReviewNotFoundError("The voice correction document was not found.")
        requested_action_id = str(document.requested_action_id or "")
        action = next(
            (
                item
                for item in claim.get("requested_actions", [])
                if isinstance(item, dict)
                and item.get("action_type") == "enter_text"
                and item.get("field_name")
                in {"incident_date", "incident_description", "incident_information"}
                and item.get("action_id") == requested_action_id
            ),
            None,
        )
        if action is None:
            return {"action": "voice_correction_already_resolved", "status": str(claim.get("status"))}
        action_field = str(action["field_name"])
        needs_date, needs_context = _voice_incident_requirements(
            claim, action_field
        )
        if document.status == "unusable":
            return {"action": "voice_correction_unusable", "status": "awaiting_documents"}

        if document.status == "validated":
            facts = dict(document.evidence_facts)
            incident_date = facts.get("incident_date")
            incident_description = facts.get("incident_description")
            injury_mentioned = facts.get("injury_mentioned") == "true"
            injury_description = facts.get("injury_description")
        else:
            result = self._voice_incident_extractor.extract(
                str(document.storage_path),
                mime_type=str(document.content_type),
                filename=document.filename,
            )
            incident_date = result.incident_date
            incident_description = (
                result.incident_description.strip()
                if result.incident_description
                else None
            )
            injury_mentioned = result.injury_mentioned
            injury_description = (
                result.injury_description if injury_mentioned else None
            )
            facts = {
                key: value
                for key, value in {
                    "incident_date": incident_date,
                    "incident_time": result.incident_time,
                    "incident_description": incident_description,
                    "injury_mentioned": "true" if injury_mentioned else "false",
                    "injury_description": injury_description,
                }.items()
                if value is not None
            }
            findings = [
                f"{key}: {value}"
                for key, value in facts.items()
                if key != "injury_description"
            ]
            if injury_mentioned:
                self._repository.record_claimant_voice_injury_signal(
                    claim_id=claim_id,
                    document_id=document_id,
                    injury_description=injury_description,
                )
            if (needs_date and not incident_date) or (
                needs_context and not incident_description
            ):
                return self._reject_voice_document(
                    claim_id, document_id, requested_action_id,
                    "The voice response did not contain the requested incident information.",
                    findings, facts,
                )
            if needs_date:
                try:
                    _validate_incident_date(str(incident_date))
                except HumanReviewConflictError:
                    return self._reject_voice_document(
                        claim_id, document_id, requested_action_id,
                        "The voice response did not contain a valid non-future incident date.",
                        findings, facts,
                    )
            self._repository.mark_document_validated(
                claim_id,
                document_id,
                quality_reason="Claimant voice supplied the requested incident information.",
                evidence_findings=findings,
                evidence_facts=facts,
            )

        review_id = str(action["review_id"])
        digest = hashlib.sha256(
            f"{claim_id}:{requested_action_id}:{document_id}".encode("utf-8")
        ).hexdigest()
        event = ClaimCorrectionReceivedEvent(
            event_id=f"{claim_id}:{requested_action_id}:voice:{digest[:16]}",
            event_type="claim.correction.received",
            claim_id=claim_id,
            correlation_id=str(
                (claim.get("voice_correction_processing") or {}).get("correlation_id")
                or f"{requested_action_id}:{digest[16:32]}"
            ),
            source="firstnotice-dispatch",
            payload=CorrectionReceivedPayload(
                review_id=review_id,
                field_name=action_field,
                source_type="claimant_voice",
                source_document_id=document_id,
                injury_mentioned=injury_mentioned,
                injury_description=injury_description,
            ),
        )
        self._repository.save_claim_voice_incident_correction(
            claim_id=claim_id,
            event_id=event.event_id,
            requested_field=action_field,
            incident_date=incident_date if needs_date else None,
            incident_description=incident_description if needs_context else None,
            correlation_id=event.correlation_id,
        )
        self._repository.mark_voice_correction_processing(
            claim_id=claim_id,
            document_id=document_id,
            requested_action_id=requested_action_id,
            status="accepted",
            correlation_id=event.correlation_id,
        )
        self._publisher.publish(event)
        return {"action": "voice_correction_processed", "status": "accepted"}

    def _reject_voice_document(
        self,
        claim_id: str,
        document_id: str,
        requested_action_id: str,
        reason: str,
        findings: list[str],
        facts: dict[str, str],
    ) -> dict[str, str]:
        self._repository.mark_document_unusable(
            claim_id,
            document_id,
            reason,
            evidence_findings=findings,
            evidence_facts=facts,
        )
        self._repository.mark_voice_correction_processing(
            claim_id=claim_id,
            document_id=document_id,
            requested_action_id=requested_action_id,
            status="unusable",
            correlation_id=None,
            message="We could not use that recording. Please re-record your answer.",
        )
        return {"action": "voice_correction_unusable", "status": "awaiting_documents"}

    def _review_for_token(self, token: str) -> HumanReviewRecord:
        if not token or len(token) > 256:
            raise HumanReviewNotFoundError("Review link is invalid.")
        review = self._repository.get_human_review_by_token_hash(hash_review_token(token))
        if review is None:
            raise HumanReviewNotFoundError("Review link is invalid.")
        if review.expires_at <= datetime.now(timezone.utc) and review.status == "pending":
            self._repository.expire_human_review(review)
            raise HumanReviewExpiredError("This review link has expired.")
        return review


class HumanReviewResumeWorkflow:
    def __init__(self, repository: FirestoreClaimRepository) -> None:
        self._repository = repository

    def resume_approved(self, claim_id: str, review_id: str, correlation_id: str) -> dict[str, str]:
        claim, review = self._validated(claim_id, review_id, "approved")
        resolved_conflicts = {
            _conflict_identity(item.model_dump(mode="python"))
            for item in review.conflicts
        }
        if resolved_conflicts:
            conflicts = [
                item for item in claim.get("conflicts", [])
                if not isinstance(item, dict)
                or _conflict_identity(item) not in resolved_conflicts
            ]
        else:
            # Backward compatibility for checkpoints created before complete
            # source/value conflict snapshots were persisted.
            legacy_fields = set(review.conflict_fields)
            conflicts = [
                item for item in claim.get("conflicts", [])
                if not isinstance(item, dict)
                or item.get("field") not in legacy_fields
            ]
        approved_fingerprints = list(dict.fromkeys([
            *[str(value) for value in claim.get("approved_issue_fingerprints", [])],
            *review.issue_fingerprints,
        ]))
        approved_now = set(review.issue_fingerprints)
        source_aware_conflicts = [
            item
            for item in claim.get("source_aware_conflicts", [])
            if not isinstance(item, dict)
            or item.get("fingerprint") not in approved_now
        ]
        source_aware_uncertainties = [
            item
            for item in claim.get("source_aware_uncertainties", [])
            if not isinstance(item, dict)
            or item.get("fingerprint") not in approved_now
        ]
        uncertainties = [
            item
            for item in claim.get("unresolved_uncertainties", [])
            if not isinstance(item, dict)
            or item.get("fingerprint") not in approved_now
        ]
        missing = list(claim.get("missing_documents", []))
        unusable = list(claim.get("unusable_evidence", []))
        target = (
            ClaimStatus.INSPECTION_PENDING
            if claim.get("status") == ClaimStatus.INSPECTION_READY
            else ClaimStatus.AWAITING_DOCUMENTS
            if missing or unusable
            else ClaimStatus.INSPECTION_PENDING
        )
        self._repository.complete_human_review_resume(
            claim_id=claim_id,
            review_id=review_id,
            target_status=target,
            conflicts=conflicts,
            missing_documents=missing,
            unusable_evidence=unusable,
            requested_actions=[],
            correlation_id=correlation_id,
            approved_issue_fingerprints=approved_fingerprints,
            source_aware_conflicts=source_aware_conflicts,
            source_aware_uncertainties=source_aware_uncertainties,
            unresolved_uncertainties=uncertainties,
        )
        return {"action": "human_review_resumed", "final_status": target.value}

    def request_correction(self, claim_id: str, review_id: str, correlation_id: str) -> dict[str, str]:
        claim, review = self._validated(claim_id, review_id, "correction_requested")
        if review.requested_evidence:
            actions = [
                UploadDocumentRequestedAction(
                    action_id=_requested_action_id(
                        review_id,
                        "upload_document",
                        f"{index}:{item.document_type}:{item.instruction}",
                    ),
                    review_id=review_id,
                    document_type=item.document_type,
                    instruction=item.instruction,
                    replaces_document_id=item.replaces_document_id,
                )
                for index, item in enumerate(review.requested_evidence)
            ]
            requested_actions = [item.model_dump(mode="python") for item in actions]
        elif review.correction_type == "upload_document":
            instruction = (review.decision_note or "").strip()
            _, document_type = _interpret_adjuster_instruction(instruction)
            action = UploadDocumentRequestedAction(
                action_id=_requested_action_id(
                    review_id, "upload_document", document_type
                ),
                review_id=review_id,
                document_type=document_type,
                instruction=instruction,
            )
            requested_actions = [action.model_dump(mode="python")]
        elif review.correction_type == "replace_document":
            target = _validated_replacement_target(
                self._repository, claim_id, review
            )
            action = UploadDocumentRequestedAction(
                action_id=_requested_action_id(
                    review_id, "upload_document", target.document_id
                ),
                review_id=review_id,
                document_type=(
                    review.recommended_remediation.document_type
                    or target.document_type
                ),
                instruction=review.recommended_remediation.instruction,
                replaces_document_id=target.document_id,
            )
            requested_actions = [action.model_dump(mode="python")]
        else:
            requested_actions = [
                _text_requested_action(review).model_dump(mode="python")
            ]
        requested_missing = [
            {
                "type": action["document_type"],
                "reason": action["instruction"],
                "source_requirement": "adjuster_request",
            }
            for action in requested_actions
            if action.get("action_type") == "upload_document"
        ]
        self._repository.complete_human_review_resume(
            claim_id=claim_id,
            review_id=review_id,
            target_status=ClaimStatus.AWAITING_DOCUMENTS,
            conflicts=list(claim.get("conflicts", [])),
            missing_documents=requested_missing
            or list(claim.get("missing_documents", [])),
            unusable_evidence=list(claim.get("unusable_evidence", [])),
            requested_actions=requested_actions,
            correlation_id=correlation_id,
        )
        return {"action": "claimant_correction_requested", "final_status": "awaiting_documents"}

    def continue_manual_handling(
        self, claim_id: str, review_id: str, correlation_id: str
    ) -> dict[str, str]:
        self._validated(claim_id, review_id, "manual_handling")
        self._repository.complete_manual_human_review(
            claim_id=claim_id,
            review_id=review_id,
            correlation_id=correlation_id,
        )
        return {
            "action": "human_review_manual_handling",
            "final_status": ClaimStatus.HUMAN_REVIEW_REQUIRED.value,
        }

    def resume_correction(
        self,
        claim_id: str,
        review_id: str,
        field_name: str,
        correlation_id: str,
        *,
        source_type: str = "claimant_manual",
        source_document_id: str | None = None,
        injury_mentioned: bool = False,
        injury_description: str | None = None,
    ) -> dict[str, str]:
        claim = self._repository.get_claim(claim_id)
        if claim is None:
            raise HumanReviewNotFoundError(f"Claim {claim_id} does not exist.")
        pending = claim.get("pending_corrections") or {}
        value = str(pending.get(field_name, "")).strip()
        if not value:
            raise HumanReviewConflictError("The requested correction was not persisted.")
        incident_date = (
            str(pending.get("incident_date") or "").strip() or None
            if source_type == "claimant_voice"
            else None
        )
        incident_description = (
            str(pending.get("incident_description") or "").strip() or None
            if source_type == "claimant_voice"
            else None
        )
        target = self._repository.complete_claim_correction(
            claim_id=claim_id,
            review_id=review_id,
            field_name=field_name,
            value=value,
            correlation_id=correlation_id,
            source_type=source_type,
            source_document_id=source_document_id,
            injury_mentioned=injury_mentioned,
            injury_description=injury_description,
            incident_date=incident_date,
            incident_description=incident_description,
        )
        return {"action": "claimant_correction_applied", "final_status": target.value}

    def _validated(self, claim_id: str, review_id: str, expected: str):
        claim = self._repository.get_claim(claim_id)
        review = self._repository.get_human_review(claim_id, review_id)
        if (
            claim is None
            or claim.get("status") not in {
                ClaimStatus.INSPECTION_READY,
                ClaimStatus.HUMAN_REVIEW_REQUIRED,
            }
            or (
                claim.get("current_human_review_id")
                and claim.get("current_human_review_id") != review_id
            )
        ):
            raise HumanReviewConflictError("Claim is not at the human review checkpoint.")
        if review is None or review.status != expected:
            raise HumanReviewConflictError("The required human review decision is absent.")
        return claim, review


def _requested_action_id(review_id: str, action_type: str, target: str) -> str:
    value = f"{review_id}:{action_type}:{target}".encode("utf-8")
    return f"ACT-{hashlib.sha256(value).hexdigest()[:16].upper()}"


def _interpret_adjuster_instruction(instruction: str) -> tuple[str, str]:
    """Map untrusted prose into one allowlisted claimant action."""
    normalized = " ".join(instruction.casefold().split())
    if not normalized:
        raise HumanReviewConflictError(
            "Describe the additional information needed from the claimant."
        )
    text_verbs = ("provide", "confirm", "correct", "enter", "update")
    if "policy number" in normalized and any(word in normalized for word in text_verbs):
        return "text", "policy_number"
    if "incident date" in normalized and any(word in normalized for word in text_verbs):
        return "text", "incident_date"
    if "upload" not in normalized:
        raise HumanReviewConflictError(
            "Request a specific supported document, vehicle photo, policy number, "
            "or incident date."
        )
    medical_terms = (
        "medical documentation",
        "medical document",
        "medical record",
        "injury documentation",
        "documentation related to the reported injury",
    )
    if any(term in normalized for term in medical_terms):
        return "upload_document", "medical_document"
    if "policy" in normalized and "document" in normalized:
        return "upload_document", "policy_document"
    if "police" in normalized and "report" in normalized:
        return "upload_document", "police_report"
    if "towing" in normalized and "receipt" in normalized:
        return "upload_document", "towing_receipt"
    if any(word in normalized for word in ("photo", "image")):
        if any(word in normalized for word in ("plate", "license", "vin", "identity")):
            return "upload_document", "license_plate_photo"
        if any(word in normalized for word in ("vehicle", "damage")):
            return "upload_document", "damage_evidence"
    raise HumanReviewConflictError(
        "Request a specific supported document, vehicle photo, policy number, "
        "or incident date."
    )


def _recommended_remediation(
    claim: dict[str, object],
    source_references: list[EvidenceSourceReference],
) -> tuple[RecommendedRemediation, str | None]:
    conflict_fields = {
        str(item.get("field"))
        for item in claim.get("conflicts", [])
        if isinstance(item, dict) and item.get("field")
    }
    if "policy_number" in conflict_fields:
        return (
            RecommendedRemediation(
                type="enter_text",
                label="Ask the claimant to confirm the policy number.",
                instruction="Please confirm your policy number.",
                field_name="policy_number",
            ),
            None,
        )
    if "incident_date" in conflict_fields:
        return (
            RecommendedRemediation(
                type="enter_text",
                label="Ask the claimant to confirm the incident date.",
                instruction="Please confirm the incident date.",
                field_name="incident_date",
            ),
            None,
        )

    source_assessments = [
        *claim.get("source_aware_conflicts", []),
        *claim.get("source_aware_uncertainties", []),
    ]
    selected_outliers = {
        str(item.get("selected_outlier_document_id"))
        for item in source_assessments
        if isinstance(item, dict) and item.get("selected_outlier_document_id")
    }
    if len(selected_outliers) == 1:
        target_id = next(iter(selected_outliers))
        target = next(
            (
                reference
                for reference in source_references
                if reference.document_id == target_id
                and reference.replacement_eligible
            ),
            None,
        )
        if target is not None:
            return (
                RecommendedRemediation(
                    type="upload_document",
                    label="Request a replacement damage photo.",
                    instruction="Please upload the correct damage photo for this claim.",
                    document_type="damage_evidence",
                ),
                target.document_id,
            )

    eligible = [
        reference
        for reference in source_references
        if reference.replacement_eligible
    ]
    if len(eligible) == 1:
        target = eligible[0]
        instruction = _replacement_instruction(target.document_type)
        label = (
            "Request a replacement damage photo."
            if target.document_type == "damage_evidence"
            else "Request a clear replacement vehicle photo."
        )
        return (
            RecommendedRemediation(
                type="upload_document",
                label=label,
                instruction=instruction,
                document_type=target.document_type,
            ),
            target.document_id,
        )
    if len(eligible) > 1:
        return (
            RecommendedRemediation(
                type="upload_document",
                label="Manual evidence selection is required.",
                instruction=(
                    "Multiple evidence artifacts may require replacement; "
                    "FirstNotice will not choose one automatically."
                ),
                can_request=False,
            ),
            None,
        )

    evidence_fields = {
        "damage_location",
        "vehicle_drivability",
        "vehicle_identity",
        "parts_affected",
    }
    if conflict_fields & evidence_fields or claim.get("unresolved_uncertainties"):
        return (
            RecommendedRemediation(
                type="upload_document",
                label="Manual evidence review is required.",
                instruction=(
                    "FirstNotice could not identify one safe evidence artifact "
                    "to replace automatically."
                ),
                can_request=False,
            ),
            None,
        )
    return (
        RecommendedRemediation(
            type="enter_text",
            label="Ask the claimant to provide corrected information.",
            instruction="Please provide the corrected incident information.",
            field_name="incident_summary",
        ),
        None,
    )


def _text_requested_action(review: HumanReviewRecord) -> EnterTextRequestedAction:
    instruction = (review.decision_note or "").strip()
    if instruction:
        action_type, field_name = _interpret_adjuster_instruction(instruction)
        if action_type != "text":
            raise HumanReviewConflictError("The recorded correction is not a text request.")
    else:
        field_name = review.recommended_remediation.field_name or "incident_summary"
        if field_name == "incident_summary":
            field_name = next(
                (
                    field
                    for field in review.conflict_fields
                    if field in {"policy_number", "incident_date"}
                ),
                field_name,
            )
        instruction = review.recommended_remediation.instruction
    return EnterTextRequestedAction(
        action_id=_requested_action_id(review.review_id, "enter_text", field_name),
        review_id=review.review_id,
        field_name=field_name,
        instruction=instruction,
    )


def _inspection_recommendation(claim: dict[str, object]) -> str:
    damage = str(claim.get("damage_type") or "").strip()
    identity_clear = not any(
        isinstance(item, dict)
        and item.get("type") in {"vehicle_identity", "license_plate_photo"}
        for item in claim.get("missing_documents", [])
    )
    details: list[str] = []
    if damage:
        details.append(f"Current evidence supports {damage.lower()}.")
    if identity_clear:
        details.append("Vehicle identity requirements are satisfied.")
    return "Physical inspection recommended. " + " ".join(details)


def _inspection_claim_snapshot(
    claim: dict[str, object], documents: list[ClaimDocument]
) -> dict[str, str | bool | None]:
    def status_for(types: set[str]) -> str:
        matching = [item for item in documents if item.document_type in types]
        if not matching:
            return "Not provided"
        return (
            "Validated"
            if any(item.status == "validated" for item in matching)
            else "Received"
        )

    return {
        "incident": str(
            claim.get("incident_summary") or claim.get("incident_description") or ""
        ) or None,
        "vehicle": str(claim.get("claim_type") or "") or None,
        "policy_number": str(claim.get("policy_number") or "") or None,
        "drivable": (
            claim.get("vehicle_drivable")
            if isinstance(claim.get("vehicle_drivable"), bool)
            else None
        ),
        "police_report_status": status_for({"police_report"}),
        "damage_evidence_status": status_for(
            {"damage_evidence", "license_plate_photo"}
        ),
    }


def _resolution_history(events: object) -> list[str]:
    if not isinstance(events, list):
        return []
    labels = {
        "document_received": "Claimant supplied additional evidence.",
        "missing_requirement_satisfied": "FirstNotice validated requested evidence.",
        "missing_requirement_still_unresolved": "FirstNotice requested another evidence attempt.",
        "claim_review_resumed": "FirstNotice re-analyzed the current evidence package.",
        "claim_moved_to_inspection_ready": "Current evidence package is ready for an inspection decision.",
    }
    result: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        label = labels.get(str(event.get("action") or ""))
        if label and label not in result:
            result.append(label)
    return result


def _comparison_document_type(
    source: str, document_types_by_source: dict[str, set[str]]
) -> dict[str, str]:
    document_types = document_types_by_source.get(source.strip().casefold(), set())
    return (
        {"document_type": next(iter(document_types))}
        if len(document_types) == 1
        else {}
    )


def _claimant_voice_update(
    document: ClaimDocument,
    claim: dict[str, object],
    review: HumanReviewRecord,
) -> dict[str, object]:
    facts = document.evidence_facts
    provenance = claim.get("incident_date_provenance")
    incident_date_document_id = (
        str(provenance.get("document_id"))
        if isinstance(provenance, dict) and provenance.get("document_id")
        else None
    )
    contributing_document_ids = {
        reference.document_id for reference in review.source_references
    }
    if incident_date_document_id:
        contributing_document_ids.add(incident_date_document_id)
    injury_source_document_id = str(
        claim.get("voice_injury_source_document_id") or ""
    )
    if injury_source_document_id:
        contributing_document_ids.add(injury_source_document_id)
    return {
        "source_label": "Claimant voice response",
        "incident_date": facts.get("incident_date"),
        "incident_time": facts.get("incident_time"),
        "incident_description": facts.get("incident_description"),
        "injury_mentioned": str(facts.get("injury_mentioned", "")).casefold()
        == "true",
        "injury_description": facts.get("injury_description"),
        "contributed_to_decision": document.document_id
        in contributing_document_ids,
    }


def _conflict_source_references(
    claim: dict[str, object], documents: list[ClaimDocument]
) -> list[EvidenceSourceReference]:
    active = [document for document in documents if document.status != "superseded"]
    references: dict[str, EvidenceSourceReference] = {}
    attributed_items: list[tuple[str, object]] = []
    attributed_items.extend(
        (str(item.get("field") or "conflict"), item.get("sources", []))
        for item in claim.get("conflicts", [])
        if isinstance(item, dict)
    )
    attributed_items.extend(
        ("unresolved_uncertainty", item.get("sources", []))
        for item in claim.get("unresolved_uncertainties", [])
        if isinstance(item, dict)
    )
    for field, sources in attributed_items:
        if not isinstance(sources, list):
            continue
        for source in sources:
            normalized = str(source).strip().casefold()
            matches = [
                document
                for document in active
                if document.filename.strip().casefold() == normalized
            ]
            if len(matches) != 1:
                continue
            document = matches[0]
            existing = references.get(document.document_id)
            fields = list(existing.conflict_fields) if existing else []
            if field and field not in fields:
                fields.append(field)
            references[document.document_id] = EvidenceSourceReference(
                document_id=document.document_id,
                filename=document.filename,
                document_type=document.document_type,
                conflict_fields=fields,
                replacement_eligible=document.document_type
                in {"damage_evidence", "license_plate_photo"},
            )
    return list(references.values())


def _current_issue_fingerprints(claim: dict[str, object]) -> list[str]:
    values = [
        str(item.get("fingerprint"))
        for item in claim.get("source_aware_conflicts", [])
        if isinstance(item, dict) and item.get("fingerprint")
    ]
    values.extend(
        str(item.get("fingerprint"))
        for item in claim.get("unresolved_uncertainties", [])
        if isinstance(item, dict) and item.get("fingerprint")
    )
    return list(dict.fromkeys(values))


def _conflict_identity(
    conflict: dict[str, object],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return (
        str(conflict.get("field") or ""),
        tuple(sorted(str(value).casefold() for value in conflict.get("values", []))),
        tuple(sorted(str(value).casefold() for value in conflict.get("sources", []))),
    )


def _legacy_reentry_generation_key(
    claim_id: str,
    review_id: str,
    events: object,
) -> str | None:
    if not isinstance(events, list):
        return None
    resumed_at = max(
        (
            item.get("timestamp")
            for item in events
            if isinstance(item, dict)
            and item.get("action") == "human_review_resumed"
            and (item.get("details") or {}).get("review_id") == review_id
            and isinstance(item.get("timestamp"), datetime)
        ),
        default=None,
    )
    if resumed_at is None:
        return None
    reentries = [
        item
        for item in events
        if isinstance(item, dict)
        and isinstance(item.get("timestamp"), datetime)
        and item["timestamp"] > resumed_at
        and (
            item.get("action") == "claim_moved_to_human_review"
            or (
                item.get("action") == "claim_review_completed"
                and item.get("to_status") == ClaimStatus.HUMAN_REVIEW_REQUIRED.value
            )
        )
    ]
    if not reentries:
        return None
    latest = max(reentries, key=lambda item: item["timestamp"])
    document_id = str(latest.get("document_id") or "").strip()
    if document_id:
        return f"{claim_id}:{document_id}:resume"
    correlation_id = str(latest.get("correlation_id") or "").strip()
    return (
        f"{claim_id}:legacy-human-review:{correlation_id}"
        if correlation_id
        else None
    )


def _validated_replacement_target(
    repository: FirestoreClaimRepository,
    claim_id: str,
    review: HumanReviewRecord,
) -> ClaimDocument:
    target = next(
        (
            item
            for item in review.source_references
            if item.document_id == review.target_document_id
            and item.replacement_eligible
        ),
        None,
    )
    if target is None:
        raise HumanReviewConflictError(
            "The replacement target is not part of this review checkpoint."
        )
    document = repository.get_document(claim_id, target.document_id)
    if (
        document is None
        or document.claim_id != claim_id
        or document.status == "superseded"
        or document.document_type != target.document_type
    ):
        raise HumanReviewConflictError(
            "The replacement target is missing, unrelated, or superseded."
        )
    return document


def _replacement_instruction(document_type: str) -> str:
    if document_type == "damage_evidence":
        return "Please upload the correct damage photo for this claim."
    return "Please upload a clear replacement photo for this claim."


def _briefing_from_claim(claim: dict[str, object]) -> HumanReviewBriefing:
    conflicts = [
        (
            f"{item.get('field')} — sources: "
            f"{', '.join(str(source) for source in item.get('sources', []))} — "
            f"{item.get('reason')}"
        )
        for item in claim.get("conflicts", [])
        if isinstance(item, dict)
    ]
    current_findings = [
        f"{item.get('source')}: {item.get('finding')}"
        for item in claim.get("current_evidence_findings", [])
        if isinstance(item, dict) and item.get("source") and item.get("finding")
    ]
    known = (
        ([f"Claim type: {claim.get('claim_type')}"] if claim.get("claim_type") else [])
        + current_findings
    )
    if not current_findings and claim.get("incident_summary"):
        known.append(str(claim["incident_summary"]).strip())
    inspection_decision = claim.get("status") == ClaimStatus.INSPECTION_READY
    reason = (
        "Autonomous intake is complete and ready for an inspection decision."
        if inspection_decision
        else str(claim.get("human_review_reason") or "Human verification is required.")
    )
    questions = [
        _conflict_review_question(item)
        for item in claim.get("conflicts", [])
        if isinstance(item, dict) and item.get("field")
    ]
    questions.extend(
        f"Resolve whether this remains consequential: {item.get('uncertainty')}"
        for item in claim.get("unresolved_uncertainties", [])
        if isinstance(item, dict)
        and item.get("uncertainty")
        and not _resolved_by_authoritative_claim_state(item, claim)
    )
    if not questions:
        questions = [
            "Review the current evidence package before authorizing inspection."
        ]
    return HumanReviewBriefing(
        reason=reason,
        summary=(
            "FirstNotice completed autonomous intake and resolved outstanding evidence requirements."
            if inspection_decision
            else f"FirstNotice paused automated routing because {reason.lower()}"
        ),
        known_facts=known,
        conflicts=conflicts,
        unresolved_questions=questions,
        recommended_next_action=questions[0],
        confidence=float(claim["review_confidence"]) if claim.get("review_confidence") is not None else None,
    )


def _resolved_by_authoritative_claim_state(
    uncertainty: dict[str, object], claim: dict[str, object]
) -> bool:
    if not claim.get("incident_date"):
        return False
    text = str(uncertainty.get("uncertainty") or "").strip().casefold()
    mentions_incident_timing = (
        "incident date" in text
        or "incident time" in text
        or ("date" in text and "incident" in text)
        or ("time" in text and "incident" in text)
    )
    describes_missing_value = any(
        phrase in text
        for phrase in (
            "not specified",
            "not provided",
            "missing",
            "unknown",
            "could not determine",
            "cannot determine",
            "unclear when",
        )
    )
    describes_conflict = any(
        phrase in text
        for phrase in ("conflict", "differ", "inconsistent", "contradict")
    )
    return (
        mentions_incident_timing
        and describes_missing_value
        and not describes_conflict
    )


def _conflict_review_question(conflict: dict[str, object]) -> str:
    field = str(conflict.get("field") or "conflicting fact")
    sources = [str(source) for source in conflict.get("sources", [])]
    if field in {"damage_location", "vehicle_drivability", "parts_affected"}:
        image_sources = [
            source
            for source in sources
            if source.lower().endswith((".jpg", ".jpeg", ".png", ".heic", ".webp"))
        ]
        if image_sources:
            return f"Verify whether {image_sources[0]} belongs to this claim."
    return f"Verify the current evidence for {field}."


def _decision_response(review: HumanReviewRecord, *, duplicate: bool) -> HumanReviewDecisionResponse:
    approved = review.status == "approved"
    return HumanReviewDecisionResponse(
        review_id=review.review_id,
        claim_id=review.claim_id,
        status=review.status,
        event_id=review.decision_event_id or human_review_event_id(
            review.claim_id, review.review_id, review.status
        ),
        message=(
            "Inspection authorized. FirstNotice has started inspection dispatch."
            if approved
            else "Manual handling recorded. No claimant action was requested."
            if review.status == "manual_handling"
            else "Correction requested. The claimant workflow will update automatically."
        ),
        duplicate=duplicate,
    )
