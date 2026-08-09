import hashlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.requested_action import EvidenceSourceReference, RequestedAction
from app.models.review_result import EvidenceConflict, UnresolvedUncertainty


def human_review_id(claim_id: str, generation: int) -> str:
    if generation < 1:
        raise ValueError("Human-review generation must be positive.")
    key = f"{claim_id}:human-review-request:v{generation}".encode("utf-8")
    return f"HRV-{hashlib.sha256(key).hexdigest()[:12].upper()}"


class HumanReviewBriefing(BaseModel):
    reason: str
    summary: str
    known_facts: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    recommended_next_action: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class RecommendedRemediation(BaseModel):
    type: Literal["enter_text", "upload_document"]
    label: str
    instruction: str
    can_request: bool = True
    field_name: str | None = None
    document_type: str | None = None


def _legacy_remediation() -> RecommendedRemediation:
    return RecommendedRemediation(
        type="enter_text",
        label="Ask the claimant to provide corrected information.",
        instruction="Please provide the corrected incident information.",
        field_name="incident_summary",
    )


class HumanReviewSourceSummary(BaseModel):
    filename: str
    document_type: str


class HumanReviewRecord(BaseModel):
    review_id: str
    claim_id: str
    status: Literal["pending", "approved", "correction_requested", "expired"]
    reason: str
    briefing: HumanReviewBriefing
    conflict_fields: list[str] = Field(default_factory=list, exclude=True)
    source_references: list[EvidenceSourceReference] = Field(default_factory=list)
    generation: int = Field(default=1, ge=1)
    generation_key: str = "legacy-cycle-1"
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    unresolved_uncertainties: list[UnresolvedUncertainty] = Field(
        default_factory=list
    )
    issue_fingerprints: list[str] = Field(default_factory=list)
    requested_actions: list[RequestedAction] = Field(default_factory=list)
    recommended_remediation: RecommendedRemediation = Field(
        default_factory=_legacy_remediation
    )
    recommended_target_document_id: str | None = None
    correction_type: Literal["text", "upload_document", "replace_document"] | None = None
    target_document_id: str | None = None
    token_hash: str = Field(min_length=64, max_length=64, exclude=True)
    created_at: datetime
    expires_at: datetime
    decision_at: datetime | None = None
    completed_at: datetime | None = None
    decision_note: str | None = None
    reviewer_label: str | None = None
    correlation_id: str
    notification_status: Literal["pending", "sent", "failed", "disabled"] = "pending"
    gmail_message_id: str | None = None
    decision_event_id: str | None = None
    decision_publish_status: Literal["pending", "published", "failed"] | None = None

    @field_validator("created_at", "expires_at", "decision_at", "completed_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Human review timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc) if value is not None else value


class HumanReviewPublicResponse(BaseModel):
    review_id: str
    claim_id: str
    status: Literal["pending", "approved", "correction_requested", "expired"]
    reason: str
    briefing: HumanReviewBriefing
    source_references: list[HumanReviewSourceSummary] = Field(default_factory=list)
    generation: int = Field(default=1, ge=1)
    recommended_remediation: RecommendedRemediation = Field(
        default_factory=_legacy_remediation
    )
    ai_recommendation: str = "Physical inspection recommended."
    claim_snapshot: dict[str, str | bool | None] = Field(default_factory=dict)
    evidence_comparison: list[dict[str, str]] = Field(default_factory=list)
    resolution_history: list[str] = Field(default_factory=list)
    expires_at: datetime
    decision_at: datetime | None = None


class HumanReviewDecisionRequest(BaseModel):
    decision_note: str | None = Field(default=None, max_length=1000)
    reviewer_label: str | None = Field(default=None, max_length=100)
    correction_type: Literal["text", "upload_document", "replace_document"] | None = None
    target_document_id: str | None = Field(default=None, max_length=128)


class HumanReviewDecisionResponse(BaseModel):
    review_id: str
    claim_id: str
    status: Literal["approved", "correction_requested"]
    event_id: str
    message: str
    duplicate: bool = False


class ClaimCorrectionRequest(BaseModel):
    field_name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=256)


class ClaimCorrectionAcceptedResponse(BaseModel):
    claim_id: str
    event_id: str
    status: str = "received"
