import re
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from google.api_core.exceptions import AlreadyExists, ServiceUnavailable

from app.models.intake_result import EvidenceFinding, IntakeResult
from app.models.claim_document import ClaimDocument
from app.models.inspection_appointment import InspectionAppointment, InspectionSlot
from app.models.notification import AdjusterNotification
from app.models.adjuster_packet import AdjusterPacket
from app.domain.claim_status import ClaimStatus
from app.models.review_result import ReviewResult
from app.tools.firestore_repository import (
    FirestoreClaimRepository,
    FirestoreWriteError,
    generate_claim_id,
    intake_result_to_claim_fields,
)


def sample_intake_result() -> IntakeResult:
    return IntakeResult(
        claim_type="auto_collision",
        damage_type="Front bumper and hood damage",
        parts_affected=["front bumper", "hood"],
        incident_summary="The submitted evidence describes a two-vehicle collision.",
        policy_number="POL-12345",
        incident_date="2026-08-01",
        vehicle_drivable=None,
        evidence_findings=[
            EvidenceFinding(
                finding="Front-end damage is visible.",
                source="accident-photo.jpg",
            )
        ],
        uncertainties=["Vehicle drivability is not stated."],
    )


class FirestoreClaimRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock(name="firestore_client")
        self.claims_collection = MagicMock(name="claims_collection")
        self.claim_ref = MagicMock(name="claim_ref")
        self.events_collection = MagicMock(name="events_collection")
        self.event_ref = MagicMock(name="event_ref")
        self.event_ref.id = "event-123"
        self.batch = MagicMock(name="batch")

        self.client.collection.return_value = self.claims_collection
        self.claims_collection.document.return_value = self.claim_ref
        self.claim_ref.collection.return_value = self.events_collection
        self.events_collection.document.return_value = self.event_ref
        self.client.batch.return_value = self.batch

        self.repository = FirestoreClaimRepository(self.client)
        self.result = sample_intake_result()

    def test_claim_id_is_readable_and_generated(self) -> None:
        first = generate_claim_id()
        second = generate_claim_id()

        self.assertRegex(first, re.compile(r"^CLM-[0-9A-F]{8}$"))
        self.assertNotEqual(first, second)

    def test_intake_result_maps_to_expected_claim_fields(self) -> None:
        mapped = intake_result_to_claim_fields(self.result)

        self.assertEqual(
            mapped,
            {
                "claim_type": "auto_collision",
                "damage_type": "Front bumper and hood damage",
                "parts_affected": ["front bumper", "hood"],
                "incident_summary": (
                    "The submitted evidence describes a two-vehicle collision."
                ),
                "policy_number": "POL-12345",
                "incident_date": "2026-08-01",
                "vehicle_drivable": None,
                "image_evidence_capabilities": [],
                "uncertainties": ["Vehicle drivability is not stated."],
            },
        )
        self.assertNotIn("evidence_findings", mapped)

    def test_completed_intake_writes_claim_and_event_in_one_batch(self) -> None:
        claim_id = self.repository.save_completed_intake(
            self.result,
            claim_id="CLM-A1B2C3D4",
            correlation_id="corr-123",
        )

        self.assertEqual(claim_id, "CLM-A1B2C3D4")
        self.client.collection.assert_called_once_with("claims")
        self.claims_collection.document.assert_called_once_with("CLM-A1B2C3D4")
        self.claim_ref.collection.assert_called_once_with("events")
        self.events_collection.document.assert_called_once_with()
        self.batch.commit.assert_called_once_with()

        claim_call = self.batch.create.call_args_list[0]
        self.assertIs(claim_call.args[0], self.claim_ref)
        claim_data = claim_call.args[1]
        self.assertEqual(claim_data["claim_id"], "CLM-A1B2C3D4")
        self.assertEqual(claim_data["status"], "intake_complete")
        self.assertEqual(claim_data["claim_type"], "auto_collision")
        self.assertEqual(claim_data["workflow_version"], "1.0")
        self.assertIsInstance(claim_data["created_at"], datetime)
        self.assertEqual(claim_data["created_at"].tzinfo, timezone.utc)
        self.assertEqual(claim_data["created_at"], claim_data["updated_at"])

        event_call = self.batch.create.call_args_list[1]
        self.assertIs(event_call.args[0], self.event_ref)
        event_data = event_call.args[1]
        self.assertEqual(event_data["action"], "claim_intake_completed")
        self.assertEqual(event_data["actor"], "firstnoticeai")
        self.assertIsNone(event_data["from_status"])
        self.assertEqual(event_data["to_status"], "intake_complete")
        self.assertEqual(event_data["correlation_id"], "corr-123")
        self.assertEqual(event_data["timestamp"].tzinfo, timezone.utc)

    def test_claim_shell_is_created_in_new_status_atomically(self) -> None:
        self.repository.create_claim_shell(
            "CLM-A1B2C3D4",
            incident_description="Rear-ended at a stoplight",
            policy_number_hint="POL-123",
            submission_event_id="event-submit",
            correlation_id="corr-submit",
        )

        claim = self.batch.create.call_args_list[0].args[1]
        event = self.batch.create.call_args_list[1].args[1]
        self.assertEqual(claim["status"], "new")
        self.assertEqual(
            claim["incident_description"], "Rear-ended at a stoplight"
        )
        self.assertEqual(event["action"], "claim_submission_received")
        self.batch.commit.assert_called_once_with()

    def test_idempotent_claim_shell_reserves_key_and_claim_atomically(self) -> None:
        reservation = self.repository.create_idempotent_claim_shell(
            "CLM-A1B2C3D4",
            idempotency_key="request-key-123",
            incident_description="Rear-ended at a stoplight",
            policy_number_hint=None,
            submission_event_id="event-submit",
            correlation_id="corr-submit",
        )

        self.assertTrue(reservation.created)
        self.assertEqual(reservation.claim_id, "CLM-A1B2C3D4")
        self.assertEqual(self.batch.create.call_count, 3)
        key_data = self.batch.create.call_args_list[0].args[1]
        self.assertEqual(key_data["status"], "processing")
        self.assertNotIn("request-key-123", key_data.values())
        self.assertEqual(len(key_data["key_hash"]), 64)
        self.batch.commit.assert_called_once_with()

    def test_duplicate_idempotency_key_returns_existing_claim(self) -> None:
        existing = MagicMock()
        existing.exists = True
        existing.to_dict.return_value = {
            "claim_id": "CLM-ORIGINAL",
            "event_id": "event-original",
            "correlation_id": "corr-original",
        }
        key_ref = MagicMock()

        def collection(name):
            if name == "claim_submission_keys":
                collection_ref = MagicMock()
                collection_ref.document.return_value = key_ref
                return collection_ref
            return self.claims_collection

        self.client.collection.side_effect = collection
        key_ref.get.return_value = existing
        self.batch.commit.side_effect = AlreadyExists("duplicate")

        reservation = self.repository.create_idempotent_claim_shell(
            "CLM-NEWVALUE",
            idempotency_key="request-key-123",
            incident_description="Rear-ended",
            policy_number_hint=None,
            submission_event_id="event-new",
            correlation_id="corr-new",
        )

        self.assertFalse(reservation.created)
        self.assertEqual(reservation.claim_id, "CLM-ORIGINAL")
        key_ref.get.assert_called_once_with()

    def test_existing_shell_intake_updates_instead_of_creating_claim(self) -> None:
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {"status": "new"}
        self.claim_ref.get.return_value = snapshot

        self.repository.complete_claim_shell_intake(
            "CLM-A1B2C3D4", self.result, correlation_id="corr-intake"
        )

        update = self.batch.update.call_args.args[1]
        event = self.batch.create.call_args.args[1]
        self.assertEqual(update["status"], "intake_complete")
        self.assertEqual(update["claim_type"], "auto_collision")
        self.assertEqual(event["from_status"], "new")
        self.assertEqual(event["to_status"], "intake_complete")
        self.batch.commit.assert_called_once_with()

    def test_append_claim_event_creates_expected_event(self) -> None:
        event_id = self.repository.append_claim_event(
            "CLM-A1B2C3D4",
            action="claim_intake_completed",
            actor="firstnoticeai",
            from_status=None,
            to_status="intake_complete",
            details={"source": "multimodal_intake"},
            correlation_id="corr-456",
        )

        self.assertEqual(event_id, "event-123")
        event = self.event_ref.create.call_args.args[0]
        self.assertEqual(event["action"], "claim_intake_completed")
        self.assertEqual(event["details"], {"source": "multimodal_intake"})
        self.assertEqual(event["correlation_id"], "corr-456")

    def test_firestore_commit_error_is_surfaced_clearly(self) -> None:
        self.batch.commit.side_effect = ServiceUnavailable("Firestore unavailable")

        with self.assertRaisesRegex(
            FirestoreWriteError, "Could not save completed intake atomically"
        ):
            self.repository.save_completed_intake(
                self.result, claim_id="CLM-A1B2C3D4"
            )

    def test_get_claim_returns_none_for_missing_document(self) -> None:
        snapshot = MagicMock()
        snapshot.exists = False
        self.claim_ref.get.return_value = snapshot

        self.assertIsNone(self.repository.get_claim("CLM-A1B2C3D4"))

    def test_review_result_updates_claim_and_event_atomically(self) -> None:
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {"status": "review_processing"}
        self.claim_ref.get.return_value = snapshot
        review = ReviewResult(
            intake_complete=True,
            intake_priority="routine",
            priority_reason="No urgent operational indicator.",
            confidence=0.9,
            inspection_required=True,
            requires_human_review=False,
        )

        status = self.repository.save_review_result(
            "CLM-A1B2C3D4", review, correlation_id="corr-review"
        )

        self.assertEqual(status, ClaimStatus.INSPECTION_PENDING)
        claim_update = self.batch.update.call_args.args[1]
        self.assertEqual(claim_update["status"], "inspection_pending")
        self.assertEqual(claim_update["review_status"], "completed")
        self.assertEqual(claim_update["missing_document_count"], 0)
        event = self.batch.create.call_args.args[1]
        self.assertEqual(event["action"], "claim_review_completed")
        self.assertEqual(event["from_status"], "review_processing")
        self.assertEqual(event["to_status"], "inspection_pending")
        self.batch.commit.assert_called_once_with()

    def test_review_firestore_write_failure_is_surfaced(self) -> None:
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {"status": "review_processing"}
        self.claim_ref.get.return_value = snapshot
        self.batch.commit.side_effect = ServiceUnavailable("Firestore unavailable")
        review = ReviewResult(
            intake_complete=True,
            intake_priority="routine",
            priority_reason="No urgent operational indicator.",
            confidence=0.9,
            inspection_required=True,
            requires_human_review=False,
        )

        with self.assertRaisesRegex(
            FirestoreWriteError, "Could not save claim review atomically"
        ):
            self.repository.save_review_result("CLM-A1B2C3D4", review)

    def test_add_document_persists_metadata_without_file_bytes(self) -> None:
        documents_collection = MagicMock()
        document_ref = MagicMock()
        documents_collection.document.return_value = document_ref
        self.claim_ref.collection.return_value = documents_collection
        document = ClaimDocument(
            document_id="DOC-A1B2C3D4",
            claim_id="CLM-A1B2C3D4",
            document_type="license_plate_photo",
            filename="plate.jpg",
            storage_path="/demo/plate.jpg",
            received_at=datetime.now(timezone.utc),
        )

        self.repository.add_document(document)

        documents_collection.document.assert_called_once_with("DOC-A1B2C3D4")
        stored = document_ref.create.call_args.args[0]
        self.assertEqual(stored["document_type"], "license_plate_photo")
        self.assertEqual(stored["storage_path"], "/demo/plate.jpg")
        self.assertNotIn("bytes", stored)
        self.assertNotIn("contents", stored)

    def test_get_documents_returns_typed_metadata(self) -> None:
        documents_collection = MagicMock()
        self.claim_ref.collection.return_value = documents_collection
        snapshot = MagicMock()
        snapshot.to_dict.return_value = {
            "document_id": "DOC-A1B2C3D4",
            "claim_id": "CLM-A1B2C3D4",
            "document_type": "license_plate_photo",
            "filename": "plate.jpg",
            "storage_path": "/demo/plate.jpg",
            "status": "received",
            "received_at": datetime.now(timezone.utc),
        }
        documents_collection.stream.return_value = [snapshot]

        documents = self.repository.get_documents("CLM-A1B2C3D4")

        self.assertEqual(len(documents), 1)
        self.assertIsInstance(documents[0], ClaimDocument)
        self.assertEqual(documents[0].document_id, "DOC-A1B2C3D4")

    def test_schedule_inspection_persists_appointment_and_events_atomically(self) -> None:
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {"status": "inspection_pending"}
        self.claim_ref.get.return_value = snapshot
        appointments_collection = MagicMock()
        appointment_ref = MagicMock()
        appointments_collection.document.return_value = appointment_ref

        def collection(name):
            return (
                appointments_collection if name == "appointments" else self.events_collection
            )

        self.claim_ref.collection.side_effect = collection
        start = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        slot = InspectionSlot(
            scheduled_start=start,
            scheduled_end=start.replace(hour=15),
        )
        appointment = InspectionAppointment(
            appointment_id="APT-A1B2C3D4",
            claim_id="CLM-A1B2C3D4",
            inspection_type="virtual",
            status="scheduled",
            scheduled_start=slot.scheduled_start,
            scheduled_end=slot.scheduled_end,
            inspector_name="Demo Inspector",
            location_type="virtual",
            location_details="Demo virtual session",
            created_at=start,
            updated_at=start,
            idempotency_key="CLM-A1B2C3D4:schedule-inspection:v1",
            calendar_provider="google_calendar",
            calendar_id="demo-calendar@group.calendar.google.com",
            calendar_event_id="calendar-event-123",
            calendar_event_link="https://calendar.google.com/event?eid=demo",
            calendar_event_created_at=start,
        )

        self.repository.schedule_inspection(
            appointment, [slot], correlation_id="corr-schedule"
        )

        self.assertEqual(self.batch.create.call_count, 4)
        self.assertIs(self.batch.create.call_args_list[0].args[0], appointment_ref)
        stored = self.batch.create.call_args_list[0].args[1]
        self.assertEqual(stored["appointment_id"], "APT-A1B2C3D4")
        self.assertEqual(stored["calendar_provider"], "google_calendar")
        self.assertEqual(stored["calendar_event_id"], "calendar-event-123")
        calendar_event = self.batch.create.call_args_list[3].args[1]
        self.assertEqual(calendar_event["action"], "google_calendar_event_created")
        claim_update = self.batch.update.call_args.args[1]
        self.assertEqual(claim_update["status"], "inspection_scheduled")
        self.batch.commit.assert_called_once_with()

    def test_create_notification_persists_mock_metadata(self) -> None:
        notifications_collection = MagicMock()
        notification_ref = MagicMock()
        notifications_collection.document.return_value = notification_ref
        self.claim_ref.collection.return_value = notifications_collection
        notification = AdjusterNotification(
            notification_id="NTF-A1B2C3D4",
            claim_id="CLM-A1B2C3D4",
            subject="Claim ready",
            message="Inspection is scheduled.",
            action_requested="Review the packet.",
            created_at=datetime.now(timezone.utc),
            idempotency_key="CLM-A1B2C3D4:dispatch:v1",
        )

        self.repository.create_notification(notification)

        notification_ref.create.assert_called_once()
        stored = notification_ref.create.call_args.args[0]
        self.assertEqual(stored["channel"], "mock_adjuster")
        self.assertEqual(stored["recipient"], "demo-adjuster")
        self.assertEqual(stored["status"], "sent")

    def test_create_notification_persists_gmail_metadata(self) -> None:
        notifications_collection = MagicMock()
        notification_ref = MagicMock()
        notifications_collection.document.return_value = notification_ref
        self.claim_ref.collection.return_value = notifications_collection
        sent_at = datetime.now(timezone.utc)
        notification = AdjusterNotification(
            notification_id="NTF-A1B2C3D4",
            claim_id="CLM-A1B2C3D4",
            channel="gmail",
            recipient="firstnotice.adjuster@gmail.com",
            subject="FirstNotice Claim Ready - CLM-A1B2C3D4",
            message="Inspection is scheduled.",
            action_requested="Review the packet.",
            created_at=sent_at,
            idempotency_key="CLM-A1B2C3D4:dispatch:v1",
            notification_provider="gmail",
            sender="firstnotice.sender@gmail.com",
            gmail_message_id="gmail-message-123",
            gmail_thread_id="gmail-thread-456",
            gmail_sent_at=sent_at,
        )

        self.repository.create_notification(notification)

        stored = notification_ref.create.call_args.args[0]
        self.assertEqual(stored["notification_provider"], "gmail")
        self.assertEqual(stored["gmail_message_id"], "gmail-message-123")
        self.assertEqual(stored["gmail_thread_id"], "gmail-thread-456")
        self.assertEqual(stored["gmail_sent_at"], sent_at)

    def test_gmail_completion_adds_email_audit_event(self) -> None:
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {
            "claim_id": "CLM-A1B2C3D4",
            "status": "inspection_scheduled",
        }
        self.claim_ref.get.return_value = snapshot
        events_collection = MagicMock()
        self.claim_ref.collection.return_value = events_collection
        events_collection.document.return_value = MagicMock()
        start = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
        appointment = InspectionAppointment(
            appointment_id="APT-A1B2C3D4",
            claim_id="CLM-A1B2C3D4",
            inspection_type="virtual",
            status="scheduled",
            scheduled_start=start,
            scheduled_end=start.replace(hour=15),
            inspector_name="Demo Inspector",
            location_type="virtual",
            created_at=start,
            updated_at=start,
            idempotency_key="CLM-A1B2C3D4:schedule-inspection:v1",
        )
        packet = AdjusterPacket(
            claim_id="CLM-A1B2C3D4",
            claim_type="auto_collision",
            incident_summary="Rear impact.",
            damage_summary="Rear bumper damage",
            intake_priority="routine",
            inspection_required=True,
            appointment_id=appointment.appointment_id,
            scheduled_inspection=start,
            human_review_required=False,
        )
        notification = AdjusterNotification(
            notification_id="NTF-A1B2C3D4",
            claim_id="CLM-A1B2C3D4",
            channel="gmail",
            recipient="firstnotice.adjuster@gmail.com",
            subject="FirstNotice Claim Ready - CLM-A1B2C3D4",
            message="Inspection is scheduled.",
            action_requested="Review the packet.",
            created_at=start,
            idempotency_key="CLM-A1B2C3D4:dispatch:v1",
            notification_provider="gmail",
            gmail_message_id="gmail-message-123",
            gmail_sent_at=start,
        )

        self.repository.complete_adjuster_dispatch(
            claim_id="CLM-A1B2C3D4",
            appointment=appointment,
            packet=packet,
            notification=notification,
            dispatch_idempotency_key="CLM-A1B2C3D4:dispatch:v1",
            correlation_id="corr-dispatch",
        )

        created_events = [call.args[1] for call in self.batch.create.call_args_list]
        email_event = next(
            event for event in created_events if event["action"] == "adjuster_email_sent"
        )
        self.assertEqual(email_event["details"]["gmail_message_id"], "gmail-message-123")
        self.assertNotIn("message", email_event["details"])


if __name__ == "__main__":
    unittest.main()
