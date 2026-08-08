from datetime import datetime

from pydantic import BaseModel, Field


class AdjusterPacket(BaseModel):
    claim_id: str
    claim_type: str
    incident_summary: str
    damage_summary: str
    intake_priority: str
    inspection_required: bool
    appointment_id: str | None
    scheduled_inspection: datetime | None
    evidence_summary: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    human_review_required: bool


class AdjusterNotificationDraft(BaseModel):
    subject: str
    message: str
    action_requested: str
