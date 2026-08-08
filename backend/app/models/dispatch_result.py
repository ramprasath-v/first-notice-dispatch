from pydantic import BaseModel, Field

from app.models.adjuster_packet import AdjusterPacket
from app.models.inspection_appointment import InspectionAppointment, InspectionSlot
from app.models.notification import AdjusterNotification


class ClaimDispatchResult(BaseModel):
    claim_id: str
    previous_status: str
    final_status: str
    candidate_slots: list[InspectionSlot] = Field(default_factory=list)
    appointment: InspectionAppointment
    adjuster_packet: AdjusterPacket
    notification: AdjusterNotification
    idempotent_replay: bool = False
