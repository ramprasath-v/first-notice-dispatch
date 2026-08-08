from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, field_validator


class InspectionSlot(BaseModel):
    scheduled_start: datetime
    scheduled_end: datetime

    @field_validator("scheduled_start", "scheduled_end")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Inspection slot timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)


class InspectionAppointment(BaseModel):
    appointment_id: str
    claim_id: str
    inspection_type: Literal["virtual", "physical"]
    status: Literal["proposed", "scheduled", "cancelled"]
    scheduled_start: datetime
    scheduled_end: datetime
    inspector_name: str
    location_type: Literal[
        "claimant_location", "inspection_center", "virtual"
    ]
    location_details: str | None = None
    created_at: datetime
    updated_at: datetime
    idempotency_key: str
    calendar_provider: Literal["google_calendar"] | None = None
    calendar_id: str | None = None
    calendar_event_id: str | None = None
    calendar_event_link: str | None = None
    calendar_event_created_at: datetime | None = None

    @field_validator(
        "scheduled_start",
        "scheduled_end",
        "created_at",
        "updated_at",
        "calendar_event_created_at",
    )
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Appointment timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc) if value is not None else value
