import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.integrations.google_calendar_service import (
    CalendarEventResult,
    GoogleCalendarError,
)
from app.integrations.gmail_service import GmailError
from app.models.adjuster_packet import AdjusterNotificationDraft, AdjusterPacket
from app.models.claim_document import ClaimDocument
from app.models.inspection_appointment import InspectionAppointment
from app.models.intake_result import intake_result_from_claim
from app.models.notification import AdjusterNotification
from app.models.review_result import review_result_from_claim
from app.services.adjuster_dispatch_service import (
    AdjusterDispatchError,
    AdjusterDispatchService,
)
from app.services.inspection_scheduling_service import (
    InspectionSchedulingService,
    dispatch_idempotency_key,
)
from app.tools.firestore_repository import FirestoreWriteError
from app.tools.notification_tools import MockAdjusterNotificationTool
from app.workflows.claim_dispatch_workflow import (
    ClaimDispatchWorkflow,
    ClaimDispatchWorkflowError,
)


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def inspection_claim(**overrides) -> dict[str, object]:
    claim = {
        "claim_id": "CLM-A1B2C3D4",
        "status": "inspection_pending",
        "claim_type": "auto_collision",
        "damage_type": "Rear bumper damage",
        "parts_affected": ["rear bumper"],
        "incident_summary": "The vehicle was struck from behind.",
        "policy_number": "POL-123",
        "incident_date": "2026-08-05",
        "vehicle_drivable": True,
        "uncertainties": [],
        "intake_complete": True,
        "intake_priority": "routine",
        "priority_reason": "No urgent operational indicator.",
        "review_confidence": 0.9,
        "inspection_required": True,
        "missing_documents": [],
        "unusable_evidence": [],
        "conflicts": [],
        "requires_human_review": False,
        "human_review_reason": None,
        "operational_indicators": {},
    }
    claim.update(overrides)
    return claim


def packet(appointment: InspectionAppointment) -> AdjusterPacket:
    return AdjusterPacket(
        claim_id="CLM-A1B2C3D4",
        claim_type="auto_collision",
        incident_summary="The vehicle was struck from behind.",
        damage_summary="Rear bumper damage",
        intake_priority="routine",
        inspection_required=True,
        appointment_id=appointment.appointment_id,
        scheduled_inspection=appointment.scheduled_start,
        evidence_summary=["damage_evidence: validated (photo.jpg)"],
        unresolved_items=[],
        conflicts=[],
        human_review_required=False,
    )


class ClaimDispatchWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.claim = inspection_claim()
        self.appointment: InspectionAppointment | None = None
        self.notification: AdjusterNotification | None = None
        self.repository = MagicMock(name="claim_repository")
        self.adjuster_service = MagicMock(name="adjuster_service")
        self.notification_tool = MagicMock(name="notification_tool")
        self.scheduler = InspectionSchedulingService()

        self.repository.get_claim.side_effect = lambda claim_id: dict(self.claim)
        self.repository.get_appointment.side_effect = (
            lambda claim_id, appointment_id: self.appointment
        )
        self.repository.get_notification.side_effect = (
            lambda claim_id, notification_id: self.notification
        )
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-1",
                claim_id="CLM-A1B2C3D4",
                document_type="damage_evidence",
                filename="photo.jpg",
                status="validated",
                received_at=NOW,
            )
        ]
        self.repository.schedule_inspection.side_effect = self._schedule
        self.repository.complete_adjuster_dispatch.side_effect = self._complete
        self.adjuster_service.build_packet.side_effect = self._build_packet
        self.adjuster_service.draft_notification.return_value = (
            AdjusterNotificationDraft(
                subject="Inspection scheduled for CLM-A1B2C3D4",
                message="Review the validated intake and scheduled inspection.",
                action_requested="Prepare for the scheduled inspection.",
            )
        )
        self.notification_tool.send_adjuster_notification.side_effect = (
            self._send_notification
        )
        self.workflow = ClaimDispatchWorkflow(
            repository=self.repository,
            scheduling_service=self.scheduler,
            adjuster_service=self.adjuster_service,
            notification_tool=self.notification_tool,
        )

    def _schedule(self, appointment, slots, **kwargs) -> None:
        self.appointment = appointment
        self.claim["status"] = "inspection_scheduled"

    def _build_packet(self, **kwargs) -> AdjusterPacket:
        return packet(kwargs["appointment"])

    def _send_notification(self, **kwargs) -> AdjusterNotification:
        self.notification = AdjusterNotification(
            notification_id=kwargs["notification_id"],
            claim_id=kwargs["claim_id"],
            subject=kwargs["draft"].subject,
            message=kwargs["draft"].message,
            action_requested=kwargs["draft"].action_requested,
            created_at=kwargs["now"],
            idempotency_key=kwargs["idempotency_key"],
        )
        return self.notification

    def _complete(self, **kwargs) -> None:
        self.claim["status"] = "adjuster_notified"
        self.claim["dispatch_idempotency_key"] = kwargs[
            "dispatch_idempotency_key"
        ]
        self.claim["adjuster_packet"] = kwargs["packet"].model_dump(mode="python")
        self.claim["adjuster_notification_id"] = kwargs[
            "notification"
        ].notification_id

    def _enable_calendar(self) -> MagicMock:
        calendar = MagicMock(name="google_calendar_service")
        calendar.create_inspection_event.return_value = CalendarEventResult(
            calendar_event_id="calendar-event-123",
            calendar_event_link="https://calendar.google.com/event?eid=demo",
            calendar_id="demo-calendar@group.calendar.google.com",
            created_at=NOW,
        )
        self.workflow = ClaimDispatchWorkflow(
            repository=self.repository,
            scheduling_service=self.scheduler,
            adjuster_service=self.adjuster_service,
            notification_tool=self.notification_tool,
            calendar_service=calendar,
        )
        return calendar

    def test_inspection_pending_creates_scheduled_appointment(self) -> None:
        result = self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.assertEqual(result.appointment.status, "scheduled")
        self.assertEqual(result.final_status, "adjuster_notified")
        self.repository.schedule_inspection.assert_called_once()

    def test_calendar_event_uses_selected_slot_and_is_persisted(self) -> None:
        calendar = self._enable_calendar()

        result = self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        requested = calendar.create_inspection_event.call_args.args[0]
        self.assertEqual(requested.scheduled_start, result.appointment.scheduled_start)
        self.assertEqual(requested.scheduled_end, result.appointment.scheduled_end)
        saved = self.repository.schedule_inspection.call_args.args[0]
        self.assertEqual(saved.calendar_provider, "google_calendar")
        self.assertEqual(saved.calendar_event_id, "calendar-event-123")
        self.assertEqual(
            saved.calendar_event_link,
            "https://calendar.google.com/event?eid=demo",
        )

    def test_duplicate_dispatch_creates_only_one_calendar_event(self) -> None:
        calendar = self._enable_calendar()

        self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)
        self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        calendar.create_inspection_event.assert_called_once()
        self.repository.schedule_inspection.assert_called_once()
        self.notification_tool.send_adjuster_notification.assert_called_once()

    def test_calendar_failure_does_not_advance_or_persist_appointment(self) -> None:
        calendar = self._enable_calendar()
        calendar.create_inspection_event.side_effect = GoogleCalendarError(
            "Calendar unavailable", retryable=True
        )

        with self.assertRaises(GoogleCalendarError):
            self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.assertEqual(self.claim["status"], "inspection_pending")
        self.repository.schedule_inspection.assert_not_called()
        self.notification_tool.send_adjuster_notification.assert_not_called()

    def test_calendar_disabled_preserves_internal_appointment(self) -> None:
        result = self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.assertIsNone(result.appointment.calendar_provider)
        self.assertIsNone(result.appointment.calendar_event_id)

    def test_appointment_is_persisted_with_expected_idempotency(self) -> None:
        self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        saved = self.repository.schedule_inspection.call_args.args[0]
        self.assertEqual(saved.claim_id, "CLM-A1B2C3D4")
        self.assertEqual(
            saved.idempotency_key,
            "CLM-A1B2C3D4:schedule-inspection:v1",
        )
        self.assertTrue(saved.appointment_id.startswith("APT-"))

    def test_routine_claim_uses_normal_afternoon_slot(self) -> None:
        slots = self.scheduler.generate_candidate_slots(now=NOW)

        selected = self.scheduler.select_slot(slots, "routine")

        self.assertEqual(selected.scheduled_start.hour, 14)

    def test_expedited_claim_uses_earliest_slot_and_physical_inspection(self) -> None:
        self.claim["intake_priority"] = "expedited"
        self.claim["vehicle_drivable"] = False

        result = self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.assertEqual(result.appointment.scheduled_start.hour, 10)
        self.assertEqual(result.appointment.inspection_type, "physical")

    def test_human_review_claim_does_not_schedule(self) -> None:
        calendar = self._enable_calendar()
        self.claim.update(
            {
                "intake_priority": "urgent_human_review",
                "requires_human_review": True,
                "human_review_reason": "Possible injury.",
            }
        )

        with self.assertRaisesRegex(
            ClaimDispatchWorkflowError, "cannot be scheduled automatically"
        ):
            self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.repository.schedule_inspection.assert_not_called()
        calendar.create_inspection_event.assert_not_called()
        self.notification_tool.send_adjuster_notification.assert_not_called()

    def test_adjuster_packet_contains_expected_structured_facts(self) -> None:
        service = AdjusterDispatchService(MagicMock(), "configured-model")
        slots = self.scheduler.generate_candidate_slots(now=NOW)
        appointment = self.scheduler.build_appointment(
            claim=self.claim,
            review_result=review_result_from_claim(self.claim),
            slot=slots[1],
            now=NOW,
        )
        built = service.build_packet(
            claim_id="CLM-A1B2C3D4",
            intake_result=intake_result_from_claim(self.claim),
            review_result=review_result_from_claim(self.claim),
            appointment=appointment,
            documents=self.repository.get_documents.return_value,
        )

        self.assertEqual(built.claim_type, "auto_collision")
        self.assertEqual(built.damage_summary, "Rear bumper damage")
        self.assertEqual(built.appointment_id, appointment.appointment_id)
        self.assertIn("damage_evidence", built.evidence_summary[0])

    def test_status_moves_to_adjuster_notified(self) -> None:
        result = self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.repository.complete_adjuster_dispatch.assert_called_once()
        self.assertEqual(result.final_status, "adjuster_notified")

    def test_duplicate_dispatch_is_idempotent(self) -> None:
        first = self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)
        second = self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.assertFalse(first.idempotent_replay)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(self.repository.schedule_inspection.call_count, 1)
        self.assertEqual(self.adjuster_service.draft_notification.call_count, 1)
        self.assertEqual(
            self.notification_tool.send_adjuster_notification.call_count, 1
        )
        self.assertEqual(self.repository.complete_adjuster_dispatch.call_count, 1)

    def test_invalid_starting_state_is_rejected(self) -> None:
        self.claim["status"] = "awaiting_documents"

        with self.assertRaisesRegex(ClaimDispatchWorkflowError, "cannot dispatch"):
            self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

    def test_firestore_failure_surfaces(self) -> None:
        self.repository.schedule_inspection.side_effect = FirestoreWriteError(
            "Could not schedule inspection atomically"
        )

        with self.assertRaises(FirestoreWriteError):
            self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

    def test_gemini_failure_leaves_resumable_scheduled_state(self) -> None:
        self.adjuster_service.draft_notification.side_effect = AdjusterDispatchError(
            "Gemini summary failed"
        )

        with self.assertRaises(AdjusterDispatchError):
            self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.assertEqual(self.claim["status"], "inspection_scheduled")
        self.assertIsNotNone(self.appointment)
        self.notification_tool.send_adjuster_notification.assert_not_called()
        self.repository.complete_adjuster_dispatch.assert_not_called()

    def test_gmail_failure_retry_reuses_calendar_and_appointment(self) -> None:
        calendar = self._enable_calendar()
        attempts = 0

        def send_with_retry(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise GmailError("Temporary Gmail failure", retryable=True)
            return self._send_notification(**kwargs)

        self.notification_tool.send_adjuster_notification.side_effect = send_with_retry

        with self.assertRaises(GmailError):
            self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.assertEqual(self.claim["status"], "inspection_scheduled")
        self.assertIsNotNone(self.appointment)
        self.repository.complete_adjuster_dispatch.assert_not_called()

        result = self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        self.assertEqual(result.final_status, "adjuster_notified")
        calendar.create_inspection_event.assert_called_once()
        self.assertEqual(self.repository.schedule_inspection.call_count, 1)
        self.assertEqual(
            self.notification_tool.send_adjuster_notification.call_count, 2
        )

    def test_notification_receives_existing_packet_and_appointment(self) -> None:
        result = self.workflow.dispatch("CLM-A1B2C3D4", now=NOW)

        sent = self.notification_tool.send_adjuster_notification.call_args.kwargs
        self.assertIs(sent["packet"], result.adjuster_packet)
        self.assertIs(sent["appointment"], result.appointment)


class MockNotificationToolTests(unittest.TestCase):
    def test_notification_is_persisted(self) -> None:
        repository = MagicMock()
        repository.get_notification.return_value = None
        tool = MockAdjusterNotificationTool(repository)
        draft = AdjusterNotificationDraft(
            subject="Claim ready",
            message="Inspection is scheduled.",
            action_requested="Review the packet.",
        )

        with self.assertLogs("app.tools.notification_tools", level="INFO") as logs:
            notification = tool.send_adjuster_notification(
                claim_id="CLM-A1B2C3D4",
                notification_id="NTF-A1B2C3D4",
                draft=draft,
                idempotency_key=dispatch_idempotency_key("CLM-A1B2C3D4"),
                now=NOW,
            )

        repository.create_notification.assert_called_once_with(notification)
        self.assertEqual(notification.recipient, "demo-adjuster")
        self.assertEqual(notification.status, "sent")
        self.assertIn("Mock adjuster notification persisted", logs.output[0])
        self.assertNotIn(draft.subject, logs.output[0])
        self.assertNotIn(draft.message, logs.output[0])


if __name__ == "__main__":
    unittest.main()
