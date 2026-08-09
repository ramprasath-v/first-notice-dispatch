from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.intake_result import EvidenceCapabilityType
from app.models.review_result import EvidenceConflict


DocumentStatus = Literal["received", "validated", "unusable", "superseded"]


class ClaimDocument(BaseModel):
    document_id: str
    claim_id: str
    document_type: str
    filename: str
    storage_path: str | None = Field(
        default=None,
        description="Local/demo path or gs:// URI; never file bytes.",
    )
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    bucket: str | None = None
    object_name: str | None = None
    gs_uri: str | None = None
    status: DocumentStatus = "received"
    received_at: datetime
    replaces_document_id: str | None = None
    requested_action_id: str | None = None
    quality_reason: str | None = None
    supported_capabilities: list[EvidenceCapabilityType] = Field(default_factory=list)
    evidence_findings: list[str] = Field(default_factory=list)
    superseded_by_document_id: str | None = None
    resume_idempotency_key: str | None = None
    resume_correlation_id: str | None = None
    resume_matched_requirement: str | None = None
    resume_started_at: datetime | None = None
    resume_extraction_result: DocumentExtractionResult | None = None
    resume_quality_processed_at: datetime | None = None
    resume_processed_at: datetime | None = None
    resume_result_status: str | None = None

    @field_validator(
        "received_at",
        "resume_started_at",
        "resume_quality_processed_at",
        "resume_processed_at",
    )
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Document timestamps must be timezone-aware UTC values.")
        return value.astimezone(timezone.utc)


class DocumentExtractionResult(BaseModel):
    usable: bool
    reason: str
    satisfies_requirement: str | None = None
    supported_capabilities: list[EvidenceCapabilityType] = Field(default_factory=list)
    evidence_findings: list[str] = Field(default_factory=list)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)


class ResumeClaimResult(BaseModel):
    claim_id: str
    document_id: str
    previous_status: str
    final_status: str
    matched_requirement: str | None
    evidence_usable: bool | None
    reason: str
    idempotent_replay: bool = False
