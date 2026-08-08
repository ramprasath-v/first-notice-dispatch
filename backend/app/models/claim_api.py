from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.claimant_evidence_requests import ClaimantEvidenceRequest


class ClaimAcceptedResponse(BaseModel):
    claim_id: str
    status: str
    event_id: str
    message: str


class DocumentAcceptedResponse(BaseModel):
    claim_id: str
    document_id: str
    status: str
    event_id: str


class ClaimSummaryResponse(BaseModel):
    claim_id: str
    status: str
    intake_priority: str | None = None
    missing_documents: list[dict[str, Any]] = Field(default_factory=list)
    requested_evidence: list[ClaimantEvidenceRequest] = Field(default_factory=list)
    requested_actions: list[dict[str, Any]] = Field(default_factory=list)
    inspection: dict[str, Any] | None = None
    updated_at: datetime


class ClaimTimelineEvent(BaseModel):
    timestamp: datetime
    action: str
    actor: str
    from_status: str | None = None
    to_status: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
