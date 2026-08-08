from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, field_validator


class AdjusterNotification(BaseModel):
    notification_id: str
    claim_id: str
    channel: Literal["mock_adjuster", "gmail"] = "mock_adjuster"
    recipient: str = "demo-adjuster"
    subject: str
    message: str
    action_requested: str
    status: Literal["sent"] = "sent"
    created_at: datetime
    idempotency_key: str
    notification_provider: Literal["gmail"] | None = None
    sender: str | None = None
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    gmail_sent_at: datetime | None = None

    @field_validator("created_at", "gmail_sent_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Notification timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc) if value is not None else value
