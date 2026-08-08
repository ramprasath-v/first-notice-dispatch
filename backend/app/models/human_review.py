from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class HumanReviewBriefing(BaseModel):
    reason: str
    summary: str
    known_facts: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    recommended_next_action: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class HumanReviewRecord(BaseModel):
    review_id: str
    claim_id: str
    status: Literal["pending", "approved", "correction_requested", "expired"]
    reason: str
    briefing: HumanReviewBriefing
    conflict_fields: list[str] = Field(default_factory=list, exclude=True)
    token_hash: str = Field(min_length=64, max_length=64, exclude=True)
    created_at: datetime
    expires_at: datetime
    decision_at: datetime | None = None
    decision_note: str | None = None
    reviewer_label: str | None = None
    correlation_id: str
    notification_status: Literal["pending", "sent", "failed", "disabled"] = "pending"
    gmail_message_id: str | None = None
    decision_event_id: str | None = None
    decision_publish_status: Literal["pending", "published", "failed"] | None = None

    @field_validator("created_at", "expires_at", "decision_at")
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
    expires_at: datetime
    decision_at: datetime | None = None


class HumanReviewDecisionRequest(BaseModel):
    decision_note: str | None = Field(default=None, max_length=1000)
    reviewer_label: str | None = Field(default=None, max_length=100)


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
