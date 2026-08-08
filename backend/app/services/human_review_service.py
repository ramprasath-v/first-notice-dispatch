import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from app.domain.claim_status import ClaimStatus
from app.events.claim_events import (
    ClaimCorrectionReceivedEvent,
    ClaimHumanReviewApprovedEvent,
    ClaimHumanReviewCorrectionRequestedEvent,
    CorrectionReceivedPayload,
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
)
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


def _review_id(claim_id: str) -> str:
    key = f"{claim_id}:human-review-request:v1".encode("utf-8")
    return f"HRV-{hashlib.sha256(key).hexdigest()[:12].upper()}"


class HumanReviewService:
    def __init__(
        self,
        *,
        repository: FirestoreClaimRepository,
        publisher: ClaimEventPublisher,
        settings: HumanReviewSettings,
        gmail_sender: GmailSender | None = None,
        recipient: str = "",
        sender: str = "",
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._settings = settings
        self._gmail_sender = gmail_sender
        self._recipient = recipient
        self._sender = sender

    def ensure_review_requested(
        self, claim_id: str, *, correlation_id: str
    ) -> HumanReviewRecord:
        claim = self._repository.get_claim(claim_id)
        if claim is None or claim.get("status") != ClaimStatus.HUMAN_REVIEW_REQUIRED:
            raise HumanReviewConflictError(
                "A human review can only be requested for human_review_required."
            )
        review_id = _review_id(claim_id)
        existing = self._repository.get_human_review(claim_id, review_id)
        token: str | None = None
        now = datetime.now(timezone.utc)
        if existing is None:
            token = secrets.token_urlsafe(32)
            review = HumanReviewRecord(
                review_id=review_id,
                claim_id=claim_id,
                status="pending",
                reason=str(claim.get("human_review_reason") or "Human verification is required."),
                briefing=_briefing_from_claim(claim),
                conflict_fields=[
                    str(item.get("field"))
                    for item in claim.get("conflicts", [])
                    if isinstance(item, dict) and item.get("field")
                ],
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
            subject=f"Human Review Required - {claim_id}",
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
            )
            raise
        self._repository.mark_human_review_notification(
            claim_id,
            review_id,
            status="sent",
            gmail_message_id=sent.gmail_message_id,
            correlation_id=correlation_id,
        )
        return review.model_copy(
            update={"notification_status": "sent", "gmail_message_id": sent.gmail_message_id}
        )

    def get_public_review(self, token: str) -> HumanReviewPublicResponse:
        review = self._review_for_token(token)
        return HumanReviewPublicResponse.model_validate(review.model_dump())

    def approve(
        self, token: str, request: HumanReviewDecisionRequest
    ) -> HumanReviewDecisionResponse:
        return self._decide(token, "approved", request)

    def request_correction(
        self, token: str, request: HumanReviewDecisionRequest
    ) -> HumanReviewDecisionResponse:
        return self._decide(token, "correction_requested", request)

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
            now=datetime.now(timezone.utc),
        )
        if review is None:
            raise HumanReviewNotFoundError("Review link is invalid.")
        if review.status == "expired":
            raise HumanReviewExpiredError("This review link has expired.")
        if duplicate and review.status not in {"approved", "correction_requested"}:
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
                ),
                actor="adjuster",
                from_status=ClaimStatus.HUMAN_REVIEW_REQUIRED.value,
                to_status=ClaimStatus.HUMAN_REVIEW_REQUIRED.value,
                details={"review_id": review.review_id},
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
        review_id = str(action["review_id"])
        event = ClaimCorrectionReceivedEvent(
            event_type="claim.correction.received",
            claim_id=claim_id,
            correlation_id=str(claim.get("correction_correlation_id") or review_id),
            source="claimant-api",
            payload=CorrectionReceivedPayload(
                review_id=review_id, field_name=field_name
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
        resolved_fields = set(review.conflict_fields)
        conflicts = [
            item for item in claim.get("conflicts", [])
            if not isinstance(item, dict) or item.get("field") not in resolved_fields
        ]
        missing = list(claim.get("missing_documents", []))
        unusable = list(claim.get("unusable_evidence", []))
        target = (
            ClaimStatus.AWAITING_DOCUMENTS
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
        )
        return {"action": "human_review_resumed", "final_status": target.value}

    def request_correction(self, claim_id: str, review_id: str, correlation_id: str) -> dict[str, str]:
        claim, review = self._validated(claim_id, review_id, "correction_requested")
        field_name = next(
            (field for field in review.conflict_fields if field in {"policy_number", "incident_date"}),
            "incident_summary",
        )
        instruction = (
            "Please confirm your policy number."
            if field_name == "policy_number"
            else "Please provide the corrected incident information."
        )
        self._repository.complete_human_review_resume(
            claim_id=claim_id,
            review_id=review_id,
            target_status=ClaimStatus.AWAITING_DOCUMENTS,
            conflicts=list(claim.get("conflicts", [])),
            missing_documents=list(claim.get("missing_documents", [])),
            unusable_evidence=list(claim.get("unusable_evidence", [])),
            requested_actions=[
                {
                    "action_type": "enter_text",
                    "field_name": field_name,
                    "instruction": instruction,
                    "review_id": review_id,
                }
            ],
            correlation_id=correlation_id,
        )
        return {"action": "claimant_correction_requested", "final_status": "awaiting_documents"}

    def resume_correction(
        self, claim_id: str, review_id: str, field_name: str, correlation_id: str
    ) -> dict[str, str]:
        claim = self._repository.get_claim(claim_id)
        if claim is None:
            raise HumanReviewNotFoundError(f"Claim {claim_id} does not exist.")
        value = str((claim.get("pending_corrections") or {}).get(field_name, "")).strip()
        if not value:
            raise HumanReviewConflictError("The requested correction was not persisted.")
        target = self._repository.complete_claim_correction(
            claim_id=claim_id,
            review_id=review_id,
            field_name=field_name,
            value=value,
            correlation_id=correlation_id,
        )
        return {"action": "claimant_correction_applied", "final_status": target.value}

    def _validated(self, claim_id: str, review_id: str, expected: str):
        claim = self._repository.get_claim(claim_id)
        review = self._repository.get_human_review(claim_id, review_id)
        if claim is None or claim.get("status") != ClaimStatus.HUMAN_REVIEW_REQUIRED:
            raise HumanReviewConflictError("Claim is not at the human review checkpoint.")
        if review is None or review.status != expected:
            raise HumanReviewConflictError("The required human review decision is absent.")
        return claim, review


def _briefing_from_claim(claim: dict[str, object]) -> HumanReviewBriefing:
    conflicts = [
        f"{item.get('field')}: {' versus '.join(str(v) for v in item.get('values', []))}"
        for item in claim.get("conflicts", [])
        if isinstance(item, dict)
    ]
    known = [
        text
        for text in (
            f"Claim type: {claim.get('claim_type')}" if claim.get("claim_type") else None,
            str(claim.get("incident_summary") or "").strip() or None,
        )
        if text
    ]
    reason = str(claim.get("human_review_reason") or "Human verification is required.")
    questions = [
        f"Verify the correct value for {item.get('field')}."
        for item in claim.get("conflicts", [])
        if isinstance(item, dict) and item.get("field")
    ] or ["Resolve the operational ambiguity described above."]
    return HumanReviewBriefing(
        reason=reason,
        summary=f"FirstNotice paused automated routing because {reason.lower()}",
        known_facts=known,
        conflicts=conflicts,
        unresolved_questions=questions,
        recommended_next_action=questions[0],
        confidence=float(claim["review_confidence"]) if claim.get("review_confidence") is not None else None,
    )


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
            "Review approved. FirstNotice has resumed processing the claim."
            if approved
            else "Correction requested. The claimant workflow will update automatically."
        ),
        duplicate=duplicate,
    )
