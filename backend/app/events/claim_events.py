from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


def _new_id() -> str:
    return str(uuid4())


class EmptyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentReceivedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=128)


class HumanReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(min_length=1, max_length=128)


class CorrectionReceivedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(min_length=1, max_length=128)
    field_name: str = Field(min_length=1, max_length=64)


class ClaimEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=_new_id, min_length=1, max_length=128)
    event_version: Literal["1"] = "1"
    claim_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    correlation_id: str = Field(
        default_factory=_new_id, min_length=1, max_length=128
    )
    source: str = Field(default="firstnotice-dispatch", min_length=1, max_length=128)

    @field_validator("occurred_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware.")
        return value.astimezone(timezone.utc)


class ClaimSubmittedEvent(ClaimEventBase):
    event_type: Literal["claim.submitted"]
    payload: EmptyPayload = Field(default_factory=EmptyPayload)


class ClaimDocumentReceivedEvent(ClaimEventBase):
    event_type: Literal["claim.document.received"]
    payload: DocumentReceivedPayload


class ClaimInspectionReadyEvent(ClaimEventBase):
    event_type: Literal["claim.inspection.ready"]
    payload: EmptyPayload = Field(default_factory=EmptyPayload)


class ClaimHumanReviewApprovedEvent(ClaimEventBase):
    event_type: Literal["claim.human_review.approved"]
    payload: HumanReviewPayload


class ClaimHumanReviewCorrectionRequestedEvent(ClaimEventBase):
    event_type: Literal["claim.human_review.correction_requested"]
    payload: HumanReviewPayload


class ClaimHumanReviewManualHandlingEvent(ClaimEventBase):
    event_type: Literal["claim.human_review.manual_handling"]
    payload: HumanReviewPayload


class ClaimCorrectionReceivedEvent(ClaimEventBase):
    event_type: Literal["claim.correction.received"]
    payload: CorrectionReceivedPayload


ClaimEvent = Annotated[
    ClaimSubmittedEvent
    | ClaimDocumentReceivedEvent
    | ClaimInspectionReadyEvent
    | ClaimHumanReviewApprovedEvent
    | ClaimHumanReviewCorrectionRequestedEvent
    | ClaimHumanReviewManualHandlingEvent
    | ClaimCorrectionReceivedEvent,
    Field(discriminator="event_type"),
]
CLAIM_EVENT_ADAPTER = TypeAdapter(ClaimEvent)


def parse_claim_event_json(data: bytes | str) -> ClaimEvent:
    return CLAIM_EVENT_ADAPTER.validate_json(data)


def inspection_ready_event_id(claim_id: str) -> str:
    return f"{claim_id}:inspection-ready:v1"


def human_review_event_id(claim_id: str, review_id: str, decision: str) -> str:
    return f"{claim_id}:{review_id}:{decision}:v1"
