from datetime import datetime, timezone
from typing import Protocol

from app.integrations.gmail_service import (
    AdjusterEmailRequest,
    GmailError,
    GmailSender,
)
from app.models.adjuster_packet import AdjusterNotificationDraft
from app.models.adjuster_packet import AdjusterPacket
from app.models.inspection_appointment import InspectionAppointment
from app.models.notification import AdjusterNotification
from app.tools.firestore_repository import FirestoreClaimRepository


class AdjusterNotificationTool(Protocol):
    def send_adjuster_notification(
        self,
        *,
        claim_id: str,
        notification_id: str,
        draft: AdjusterNotificationDraft,
        packet: AdjusterPacket,
        appointment: InspectionAppointment,
        idempotency_key: str,
        now: datetime,
    ) -> AdjusterNotification: ...


class MockAdjusterNotificationTool:
    """Persists and prints a deterministic local adjuster notification."""

    def __init__(self, repository: FirestoreClaimRepository) -> None:
        self._repository = repository

    def send_adjuster_notification(
        self,
        *,
        claim_id: str,
        notification_id: str,
        draft: AdjusterNotificationDraft,
        packet: AdjusterPacket | None = None,
        appointment: InspectionAppointment | None = None,
        idempotency_key: str,
        now: datetime,
    ) -> AdjusterNotification:
        existing = self._repository.get_notification(claim_id, notification_id)
        if existing is not None:
            return existing

        if now.tzinfo is None:
            raise ValueError("Notification time must be timezone-aware.")
        notification = AdjusterNotification(
            notification_id=notification_id,
            claim_id=claim_id,
            subject=draft.subject,
            message=draft.message,
            action_requested=draft.action_requested,
            created_at=now.astimezone(timezone.utc),
            idempotency_key=idempotency_key,
        )
        self._repository.create_notification(notification)
        print("\nMock adjuster notification")
        print(f"to: {notification.recipient}")
        print(f"subject: {notification.subject}")
        print(notification.message)
        return notification


class GmailAdjusterNotificationTool:
    """Maps the existing packet/draft to Gmail and persists delivery metadata."""

    def __init__(
        self,
        repository: FirestoreClaimRepository,
        gmail_sender: GmailSender,
        *,
        recipient: str,
        sender: str,
    ) -> None:
        self._repository = repository
        self._gmail_sender = gmail_sender
        self._recipient = recipient
        self._sender = sender

    def send_adjuster_notification(
        self,
        *,
        claim_id: str,
        notification_id: str,
        draft: AdjusterNotificationDraft,
        packet: AdjusterPacket,
        appointment: InspectionAppointment,
        idempotency_key: str,
        now: datetime,
    ) -> AdjusterNotification:
        existing = self._repository.get_notification(claim_id, notification_id)
        if existing is not None:
            return existing
        request = AdjusterEmailRequest(
            notification_id=notification_id,
            claim_id=claim_id,
            recipient=self._recipient,
            sender=self._sender,
            subject=f"FirstNotice Claim Ready - {claim_id}",
            adjuster_summary=draft.message,
            incident_summary=packet.incident_summary,
            intake_priority=packet.intake_priority,
            inspection_start=appointment.scheduled_start,
            inspection_end=appointment.scheduled_end,
            inspection_location=appointment.location_details
            or appointment.location_type,
            inspection_type=appointment.inspection_type,
            calendar_event_link=appointment.calendar_event_link,
            evidence_summary=packet.evidence_summary,
            unresolved_notes=[*packet.unresolved_items, *packet.conflicts],
            action_requested=draft.action_requested,
        )
        try:
            result = self._gmail_sender.send_adjuster_email(request)
        except GmailError as exc:
            try:
                self._repository.append_claim_event(
                    claim_id,
                    action="adjuster_email_failed",
                    actor="gmail",
                    from_status="inspection_scheduled",
                    to_status="inspection_scheduled",
                    details={
                        "notification_id": notification_id,
                        "retryable": exc.retryable,
                    },
                    correlation_id=idempotency_key,
                )
            except Exception:
                # The Pub/Sub failure record remains authoritative; never mask Gmail.
                pass
            raise
        notification = AdjusterNotification(
            notification_id=notification_id,
            claim_id=claim_id,
            channel="gmail",
            recipient=result.recipient,
            subject=request.subject,
            message=draft.message,
            action_requested=draft.action_requested,
            status="sent",
            created_at=now.astimezone(timezone.utc),
            idempotency_key=idempotency_key,
            notification_provider="gmail",
            sender=result.sender,
            gmail_message_id=result.gmail_message_id,
            gmail_thread_id=result.gmail_thread_id,
            gmail_sent_at=result.sent_at,
        )
        self._repository.create_notification(notification)
        return notification
