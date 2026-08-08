from typing import Literal

from pydantic import BaseModel, Field


class EvidenceInput(BaseModel):
    path: str
    document_type: str
    content_type: str | None = None


class ClaimStateResult(BaseModel):
    claim_id: str
    status: str
    missing_documents: list[str] = Field(default_factory=list)
    requires_human_review: bool = False


class CoordinatorResult(BaseModel):
    claim_id: str
    initial_status: str
    final_status: str
    selected_actions: list[str] = Field(default_factory=list)
    stop_reason: Literal[
        "awaiting_external_evidence",
        "human_review_required",
        "workflow_complete",
        "event_boundary_reached",
    ]
