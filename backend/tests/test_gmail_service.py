import base64
import unittest
from datetime import datetime, timezone
from email import message_from_bytes, policy
from unittest.mock import MagicMock, patch

from requests import Response

from app.events.claim_event_handler import _is_retryable
from app.integrations.gmail_service import (
    AdjusterEmailRequest,
    GmailConfigurationError,
    GmailError,
    GmailSendResult,
    GmailService,
    GmailSettings,
)
from app.models.adjuster_packet import AdjusterNotificationDraft, AdjusterPacket
from app.models.inspection_appointment import InspectionAppointment
from app.tools.notification_tools import GmailAdjusterNotificationTool


NOW = datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def api_response(status: int, payload: dict[str, object] | None = None) -> MagicMock:
    response = MagicMock(spec=Response)
    response.status_code = status
    response.json.return_value = payload or {}
    return response


def email_request() -> AdjusterEmailRequest:
    return AdjusterEmailRequest(
        notification_id="NTF-A1B2C3D4",
        claim_id="CLM-A1B2C3D4",
        recipient="firstnotice.adjuster@gmail.com",
        sender="firstnotice.sender@gmail.com",
        subject="FirstNotice Claim Ready - CLM-A1B2C3D4",
        adjuster_summary="Review the validated intake and inspection plan.",
        incident_summary="The vehicle was struck from behind.",
        intake_priority="routine",
        inspection_start=START,
        inspection_end=END,
        inspection_location="Secure virtual inspection session",
        inspection_type="virtual",
        calendar_event_link="https://calendar.google.com/event?eid=demo",
        evidence_summary=["damage_evidence: validated (photo.jpg)"],
        unresolved_notes=[],
        action_requested="Review the handoff.",
    )


def appointment() -> InspectionAppointment:
    return InspectionAppointment(
        appointment_id="APT-A1B2C3D4",
        claim_id="CLM-A1B2C3D4",
        inspection_type="virtual",
        status="scheduled",
        scheduled_start=START,
        scheduled_end=END,
        inspector_name="Demo Inspector",
        location_type="virtual",
        location_details="Secure virtual inspection session",
        created_at=NOW,
        updated_at=NOW,
        idempotency_key="CLM-A1B2C3D4:schedule-inspection:v1",
        calendar_provider="google_calendar",
        calendar_id="demo-calendar@group.calendar.google.com",
        calendar_event_id="calendar-event-123",
        calendar_event_link="https://calendar.google.com/event?eid=demo",
        calendar_event_created_at=NOW,
    )


def packet() -> AdjusterPacket:
    return AdjusterPacket(
        claim_id="CLM-A1B2C3D4",
        claim_type="auto_collision",
        incident_summary="The vehicle was struck from behind.",
        damage_summary="Rear bumper damage",
        intake_priority="routine",
        inspection_required=True,
        appointment_id="APT-A1B2C3D4",
        scheduled_inspection=START,
        evidence_summary=["damage_evidence: validated (photo.jpg)"],
        unresolved_items=[],
        conflicts=[],
        human_review_required=False,
    )


class GmailServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.service = GmailService(session=self.session)

    def test_send_builds_operational_email_and_returns_provider_ids(self) -> None:
        self.session.post.return_value = api_response(
            200, {"id": "gmail-message-123", "threadId": "gmail-thread-456"}
        )

        result = self.service.send_adjuster_email(email_request())

        raw = self.session.post.call_args.kwargs["json"]["raw"]
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
        message = message_from_bytes(decoded, policy=policy.default)
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertEqual(message["To"], "firstnotice.adjuster@gmail.com")
        self.assertIn("CLM-A1B2C3D4", message["Subject"])
        self.assertIn(START.isoformat(), body)
        self.assertIn("https://calendar.google.com/event?eid=demo", body)
        self.assertIn("Review the validated intake", body)
        self.assertNotIn("POL-", body)
        self.assertEqual(result.gmail_message_id, "gmail-message-123")
        self.assertEqual(result.gmail_thread_id, "gmail-thread-456")

    def test_retryable_and_non_retryable_errors_are_classified(self) -> None:
        self.session.post.return_value = api_response(503)
        with self.assertRaises(GmailError) as retryable:
            self.service.send_adjuster_email(email_request())
        self.assertTrue(retryable.exception.retryable)
        self.assertTrue(_is_retryable(retryable.exception))

        self.session.post.return_value = api_response(403)
        with self.assertRaises(GmailError) as permanent:
            self.service.send_adjuster_email(email_request())
        self.assertFalse(permanent.exception.retryable)
        self.assertFalse(_is_retryable(permanent.exception))


class GmailNotificationToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.repository.get_notification.return_value = None
        self.sender = MagicMock()
        self.sender.send_adjuster_email.return_value = GmailSendResult(
            gmail_message_id="gmail-message-123",
            gmail_thread_id="gmail-thread-456",
            sent_at=NOW,
            recipient="firstnotice.adjuster@gmail.com",
            sender="firstnotice.sender@gmail.com",
        )
        self.tool = GmailAdjusterNotificationTool(
            self.repository,
            self.sender,
            recipient="firstnotice.adjuster@gmail.com",
            sender="firstnotice.sender@gmail.com",
        )
        self.draft = AdjusterNotificationDraft(
            subject="Existing draft subject",
            message="Review the validated intake and inspection plan.",
            action_requested="Review the handoff.",
        )

    def send(self):
        return self.tool.send_adjuster_notification(
            claim_id="CLM-A1B2C3D4",
            notification_id="NTF-A1B2C3D4",
            draft=self.draft,
            packet=packet(),
            appointment=appointment(),
            idempotency_key="CLM-A1B2C3D4:dispatch:v1",
            now=NOW,
        )

    def test_configured_gmail_send_persists_delivery_metadata(self) -> None:
        notification = self.send()

        request = self.sender.send_adjuster_email.call_args.args[0]
        self.assertEqual(request.recipient, "firstnotice.adjuster@gmail.com")
        self.assertIn("CLM-A1B2C3D4", request.subject)
        self.assertEqual(notification.notification_provider, "gmail")
        self.assertEqual(notification.gmail_message_id, "gmail-message-123")
        self.assertEqual(notification.gmail_thread_id, "gmail-thread-456")
        self.repository.create_notification.assert_called_once_with(notification)

    def test_completed_notification_is_idempotent(self) -> None:
        existing = MagicMock()
        self.repository.get_notification.return_value = existing

        self.assertIs(self.send(), existing)
        self.sender.send_adjuster_email.assert_not_called()
        self.repository.create_notification.assert_not_called()

    def test_failure_records_safe_event_without_persisting_notification(self) -> None:
        self.sender.send_adjuster_email.side_effect = GmailError(
            "Gmail unavailable", retryable=True
        )

        with self.assertRaises(GmailError):
            self.send()

        self.repository.create_notification.assert_not_called()
        event = self.repository.append_claim_event.call_args.kwargs
        self.assertEqual(event["action"], "adjuster_email_failed")
        self.assertEqual(event["details"]["retryable"], True)
        self.assertNotIn("Gmail unavailable", str(event))


class GmailSettingsTests(unittest.TestCase):
    @patch.dict("os.environ", {"GMAIL_NOTIFICATION_ENABLED": "false"}, clear=True)
    def test_disabled_requires_no_oauth_secrets(self) -> None:
        self.assertEqual(GmailSettings.from_env(), GmailSettings(enabled=False))

    @patch.dict(
        "os.environ",
        {
            "GMAIL_NOTIFICATION_ENABLED": "true",
            "ADJUSTER_EMAIL": "firstnotice.adjuster@gmail.com",
        },
        clear=True,
    )
    def test_enabled_requires_sender_and_oauth_secrets(self) -> None:
        with self.assertRaises(GmailConfigurationError):
            GmailSettings.from_env()


if __name__ == "__main__":
    unittest.main()
