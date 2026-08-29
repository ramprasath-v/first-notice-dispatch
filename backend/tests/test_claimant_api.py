import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.agents.firstnotice_adk import build_firstnotice_coordinator
from app.api.pubsub import create_app
from app.events.coordinator_invoker import AdkClaimCoordinatorInvoker
from app.events.claim_events import (
    ClaimDocumentReceivedEvent,
    ClaimSubmittedEvent,
)
from app.models.claim_api import (
    ClaimAcceptedResponse,
    ClaimSummaryResponse,
    ClaimTimelineEvent,
    DocumentAcceptedResponse,
)
from app.models.claim_document import ClaimDocument
from app.models.adk_orchestration import ClaimStateResult, EvidenceInput
from app.models.intake_result import IntakeResult
from app.models.human_review import ClaimCorrectionAcceptedResponse
from app.services.claim_storage_service import (
    ClaimStorageError,
    ClaimStorageService,
    ClaimStorageValidationError,
    GcsSettings,
    StoredClaimObject,
    ValidatedUpload,
    sanitize_filename,
)
from app.services.intake_extraction_service import evidence_part
from app.services.claim_submission_service import (
    ClaimNotFoundError,
    ClaimSubmissionError,
    ClaimSubmissionService,
    EvidenceUpload,
)
from app.tools.firestore_repository import ClaimSubmissionReservation
from app.tools.firestore_repository import ReplacementUploadReservation
from app.models.requested_action import UploadDocumentRequestedAction


CLAIM_ID = "CLM-A1B2C3D4"
DOCUMENT_ID = "DOC-A1B2C3D4"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def evidence(
    data: bytes = b"image-bytes",
    *,
    filename: str = "accident.jpg",
    content_type: str = "image/jpeg",
) -> EvidenceUpload:
    return EvidenceUpload(
        file_obj=BytesIO(data),
        filename=filename,
        content_type=content_type,
    )


class ClaimStorageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock()
        self.bucket = MagicMock()
        self.blob = MagicMock()
        self.client.bucket.return_value = self.bucket
        self.bucket.blob.return_value = self.blob
        self.service = ClaimStorageService(
            GcsSettings("demo-project", "claim-bucket"),
            client=self.client,
            max_size_bytes=20,
        )

    def test_upload_uses_expected_deterministic_object_path(self) -> None:
        file_obj = BytesIO(b"image-bytes")
        upload = self.service.validate_upload(
            file_obj,
            filename="accident.jpg",
            content_type="image/jpeg",
        )

        stored = self.service.upload_claim_document(
            claim_id=CLAIM_ID,
            document_id=DOCUMENT_ID,
            file_obj=file_obj,
            upload=upload,
        )

        expected = f"claims/{CLAIM_ID}/documents/{DOCUMENT_ID}/accident.jpg"
        self.bucket.blob.assert_called_once_with(expected)
        self.blob.upload_from_file.assert_called_once()
        self.assertEqual(stored.object_name, expected)
        self.assertEqual(stored.gs_uri, f"gs://claim-bucket/{expected}")

    def test_unsupported_mime_type_is_rejected(self) -> None:
        with self.assertRaises(ClaimStorageValidationError):
            self.service.validate_upload(
                BytesIO(b"text"), filename="notes.txt", content_type="text/plain"
            )

    def test_oversized_file_is_rejected(self) -> None:
        with self.assertRaisesRegex(ClaimStorageValidationError, "exceeds"):
            self.service.validate_upload(
                BytesIO(b"x" * 21),
                filename="large.jpg",
                content_type="image/jpeg",
            )

    def test_filename_sanitization_removes_paths_and_unsafe_characters(self) -> None:
        self.assertEqual(
            sanitize_filename("../../private\\folder/My accident (1).jpg"),
            "My_accident_1_.jpg",
        )

    def test_gcs_evidence_uses_supported_genai_uri_part(self) -> None:
        part = evidence_part(
            "gs://claim-bucket/claims/CLM/documents/DOC/photo.jpg",
            mime_type="image/jpeg",
        )

        self.assertEqual(
            part.file_data.file_uri,
            "gs://claim-bucket/claims/CLM/documents/DOC/photo.jpg",
        )
        self.assertEqual(part.file_data.mime_type, "image/jpeg")


class GcsBackedAdkIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_shell_runs_intake_without_creating_second_claim(self) -> None:
        intake = IntakeResult(
            claim_type="auto_collision",
            damage_type="Rear bumper damage",
            parts_affected=["rear bumper"],
            incident_summary="Rear-ended at a stoplight.",
            policy_number="POL-123",
            incident_date="2026-08-07",
            vehicle_drivable=True,
        )
        extraction = MagicMock()
        extraction.extract.return_value = intake
        tools = MagicMock()
        tools.get_claim_state.side_effect = [
            ClaimStateResult(claim_id=CLAIM_ID, status="new"),
            ClaimStateResult(claim_id=CLAIM_ID, status="intake_complete"),
            ClaimStateResult(claim_id=CLAIM_ID, status="awaiting_documents"),
        ]
        tools.get_claim_evidence_inputs.return_value = [
            EvidenceInput(
                path="gs://claim-bucket/claims/CLM/document/photo.jpg",
                document_type="damage_evidence",
                content_type="image/jpeg",
            )
        ]
        tools.get_claim_intake_context.return_value = {
            "incident_description": "Rear-ended at a stoplight",
            "policy_number_hint": "POL-123",
        }
        tools.complete_claim_intake.return_value = ClaimStateResult(
            claim_id=CLAIM_ID, status="intake_complete"
        )
        tools.run_claim_review.return_value = ClaimStateResult(
            claim_id=CLAIM_ID,
            status="awaiting_documents",
            missing_documents=["license_plate_photo"],
        )
        coordinator = build_firstnotice_coordinator(
            extraction_service=extraction,
            workflow_tools=tools,
        )

        await AdkClaimCoordinatorInvoker(coordinator).process_submitted_claim(CLAIM_ID)

        extraction.extract.assert_called_once_with(
            ["gs://claim-bucket/claims/CLM/document/photo.jpg"],
            incident_description="Rear-ended at a stoplight",
            policy_number_hint="POL-123",
        )
        tools.complete_claim_intake.assert_called_once_with(CLAIM_ID, intake)
        tools.create_claim_record.assert_not_called()


class ClaimSubmissionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.storage = MagicMock()
        self.publisher = MagicMock()
        self.publisher.publish.return_value = "pubsub-message-123"
        self.repository.create_idempotent_claim_shell.return_value = (
            ClaimSubmissionReservation(
                claim_id=CLAIM_ID,
                event_id="event-submit",
                correlation_id="corr-submit",
                created=True,
            )
        )
        self.service = ClaimSubmissionService(
            repository=self.repository,
            storage_service=self.storage,
            publisher=self.publisher,
        )

        def validate(file_obj, *, filename, content_type):
            size = len(file_obj.getvalue())
            return ValidatedUpload(
                filename=filename,
                content_type=content_type,
                size_bytes=size,
            )

        def upload(*, claim_id, document_id, file_obj, upload):
            object_name = (
                f"claims/{claim_id}/documents/{document_id}/{upload.filename}"
            )
            return StoredClaimObject(
                **upload.model_dump(),
                bucket="claim-bucket",
                object_name=object_name,
                gs_uri=f"gs://claim-bucket/{object_name}",
                document_id=document_id,
            )

        self.storage.validate_upload.side_effect = validate
        self.storage.upload_claim_document.side_effect = upload

    @patch(
        "app.services.claim_submission_service.generate_document_id",
        return_value=DOCUMENT_ID,
    )
    @patch(
        "app.services.claim_submission_service.generate_claim_id",
        return_value=CLAIM_ID,
    )
    def test_valid_submission_persists_shell_metadata_and_event(
        self, _claim_id, _document_id
    ) -> None:
        response = self.service.submit_claim(
            incident_description="Rear-ended at a stoplight",
            policy_number_hint="POL-123",
            evidence=[evidence()],
            idempotency_key="request-key-123",
        )

        self.assertEqual(response.claim_id, CLAIM_ID)
        shell = self.repository.create_idempotent_claim_shell.call_args
        self.assertEqual(shell.args[0], CLAIM_ID)
        self.assertEqual(
            shell.kwargs["incident_description"], "Rear-ended at a stoplight"
        )
        document = self.repository.add_document.call_args.args[0]
        self.assertEqual(document.document_id, DOCUMENT_ID)
        self.assertEqual(document.document_type, "damage_evidence")
        self.assertEqual(document.content_type, "image/jpeg")
        self.assertEqual(document.size_bytes, len(b"image-bytes"))
        self.assertTrue(document.storage_path.startswith("gs://claim-bucket/claims/"))
        published = self.publisher.publish.call_args.args[0]
        self.assertIsInstance(published, ClaimSubmittedEvent)
        self.assertEqual(published.claim_id, CLAIM_ID)
        self.repository.mark_claim_submission_published.assert_called_once()

    @patch(
        "app.services.claim_submission_service.generate_document_id",
        return_value=DOCUMENT_ID,
    )
    @patch(
        "app.services.claim_submission_service.generate_claim_id",
        return_value=CLAIM_ID,
    )
    def test_upload_failure_does_not_publish(
        self, _claim_id, _document_id
    ) -> None:
        self.storage.upload_claim_document.side_effect = ClaimStorageError("GCS down")

        with self.assertRaises(ClaimSubmissionError):
            self.service.submit_claim(
                incident_description="Rear-ended",
                policy_number_hint=None,
                evidence=[evidence()],
                idempotency_key="request-key-123",
            )

        self.publisher.publish.assert_not_called()
        self.repository.mark_claim_submission_upload_failed.assert_called_once()

    def test_duplicate_submission_returns_original_without_upload_or_publish(self) -> None:
        self.repository.create_idempotent_claim_shell.side_effect = [
            ClaimSubmissionReservation(
                claim_id=CLAIM_ID,
                event_id="event-original",
                correlation_id="corr-original",
                created=True,
            ),
            ClaimSubmissionReservation(
                claim_id=CLAIM_ID,
                event_id="event-original",
                correlation_id="corr-original",
                created=False,
            ),
        ]

        with patch(
            "app.services.claim_submission_service.generate_claim_id",
            return_value=CLAIM_ID,
        ):
            first = self.service.submit_claim(
                incident_description="Rear-ended",
                policy_number_hint=None,
                evidence=[evidence()],
                idempotency_key="same-request-key",
            )
            response = self.service.submit_claim(
                incident_description="Rear-ended",
                policy_number_hint=None,
                evidence=[evidence()],
                idempotency_key="same-request-key",
            )

        self.assertEqual(first.claim_id, response.claim_id)
        self.assertEqual(response.claim_id, CLAIM_ID)
        self.assertEqual(response.event_id, "event-original")
        self.storage.upload_claim_document.assert_called_once()
        self.repository.add_document.assert_called_once()
        self.publisher.publish.assert_called_once()

    def test_different_submission_keys_create_separate_claims(self) -> None:
        reservations = [
            ClaimSubmissionReservation("CLM-11111111", "evt-1", "corr-1", True),
            ClaimSubmissionReservation("CLM-22222222", "evt-2", "corr-2", True),
        ]
        self.repository.create_idempotent_claim_shell.side_effect = reservations

        with patch(
            "app.services.claim_submission_service.generate_claim_id",
            side_effect=["CLM-11111111", "CLM-22222222"],
        ):
            first = self.service.submit_claim(
                incident_description="First",
                policy_number_hint=None,
                evidence=[evidence()],
                idempotency_key="request-key-one",
            )
            second = self.service.submit_claim(
                incident_description="Second",
                policy_number_hint=None,
                evidence=[evidence()],
                idempotency_key="request-key-two",
            )

        self.assertNotEqual(first.claim_id, second.claim_id)
        self.assertEqual(self.storage.upload_claim_document.call_count, 2)
        self.assertEqual(self.publisher.publish.call_count, 2)

    @patch(
        "app.services.claim_submission_service.generate_document_id",
        return_value=DOCUMENT_ID,
    )
    def test_missing_document_persists_metadata_and_publishes_event(
        self, _document_id
    ) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
        }

        response = self.service.add_missing_document(
            claim_id=CLAIM_ID,
            document_type="license_plate_photo",
            evidence=evidence(filename="plate.png", content_type="image/png"),
        )

        self.assertEqual(response.document_id, DOCUMENT_ID)
        document = self.repository.add_document.call_args.args[0]
        self.assertEqual(document.document_type, "license_plate_photo")
        published = self.publisher.publish.call_args.args[0]
        self.assertIsInstance(published, ClaimDocumentReceivedEvent)
        self.assertEqual(published.payload.document_id, DOCUMENT_ID)

    def test_missing_document_rejects_unknown_claim(self) -> None:
        self.repository.get_claim.return_value = None

        with self.assertRaises(ClaimNotFoundError):
            self.service.add_missing_document(
                claim_id=CLAIM_ID,
                document_type="license_plate_photo",
                evidence=evidence(),
            )

        self.storage.upload_claim_document.assert_not_called()
        self.publisher.publish.assert_not_called()

    def test_requested_action_resolves_damage_replacement_server_side(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
        }
        action = UploadDocumentRequestedAction(
            action_id="ACT-REPLACE",
            review_id="HRV-1",
            document_type="damage_evidence",
            instruction="Upload the correct damage photo.",
            replaces_document_id="DOC-OLD",
        )
        self.repository.reserve_replacement_upload.return_value = (
            ReplacementUploadReservation(
                action=action,
                document_id="DOC-REPLACEMENT",
                event_id="replacement-event",
                correlation_id="replacement-correlation",
                status="uploading",
                should_upload=True,
            )
        )

        response = self.service.add_missing_document(
            claim_id=CLAIM_ID,
            document_type="ignored_by_server",
            requested_action_id="ACT-REPLACE",
            idempotency_key="replacement-key-1",
            evidence=evidence(filename="correct-damage.jpg"),
        )

        document = self.repository.add_document.call_args.args[0]
        self.assertEqual(response.document_id, "DOC-REPLACEMENT")
        self.assertEqual(document.document_type, "damage_evidence")
        self.assertEqual(document.requested_action_id, "ACT-REPLACE")
        self.assertEqual(document.replaces_document_id, "DOC-OLD")
        self.assertEqual(
            self.publisher.publish.call_args.args[0].event_id,
            "replacement-event",
        )

    def test_authorized_medical_requested_action_persists_attachment_type(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
        }
        action = UploadDocumentRequestedAction(
            action_id="ACT-MEDICAL",
            review_id="HRV-INJURY",
            document_type="medical_document",
            instruction="Please upload medical documentation.",
        )
        self.repository.reserve_replacement_upload.return_value = (
            ReplacementUploadReservation(
                action=action,
                document_id="DOC-MEDICAL",
                event_id="medical-event",
                correlation_id="medical-correlation",
                status="uploading",
                should_upload=True,
            )
        )

        response = self.service.add_missing_document(
            claim_id=CLAIM_ID,
            document_type="ignored_by_server",
            requested_action_id="ACT-MEDICAL",
            idempotency_key="medical-upload-key",
            evidence=evidence(
                filename="medical-record.pdf", content_type="application/pdf"
            ),
        )

        document = self.repository.add_document.call_args.args[0]
        self.assertEqual(response.document_id, "DOC-MEDICAL")
        self.assertEqual(document.document_type, "medical_document")
        self.assertEqual(document.requested_action_id, "ACT-MEDICAL")
        self.assertIsNone(document.replaces_document_id)

    def test_requested_action_carries_policy_replacement_target(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
        }
        action = UploadDocumentRequestedAction(
            action_id="ACT-POLICY-REPLACE",
            review_id="AUTONOMOUS-POLICY",
            document_type="policy_document",
            instruction="Upload the correct policy document.",
            replaces_document_id="DOC-OLD-POLICY",
        )
        self.repository.reserve_replacement_upload.return_value = (
            ReplacementUploadReservation(
                action=action,
                document_id="DOC-NEW-POLICY",
                event_id="policy-replacement-event",
                correlation_id="policy-replacement-correlation",
                status="uploading",
                should_upload=True,
            )
        )

        self.service.add_missing_document(
            claim_id=CLAIM_ID,
            document_type="ignored_by_server",
            requested_action_id="ACT-POLICY-REPLACE",
            idempotency_key="policy-replacement-key",
            evidence=evidence(
                filename="correct-policy.pdf",
                content_type="application/pdf",
            ),
        )

        document = self.repository.add_document.call_args.args[0]
        self.assertEqual(document.document_type, "policy_document")
        self.assertEqual(document.requested_action_id, "ACT-POLICY-REPLACE")
        self.assertEqual(document.replaces_document_id, "DOC-OLD-POLICY")

    def test_duplicate_replacement_upload_returns_same_effective_document(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
        }
        action = UploadDocumentRequestedAction(
            action_id="ACT-REPLACE",
            review_id="HRV-1",
            document_type="damage_evidence",
            instruction="Upload the correct damage photo.",
            replaces_document_id="DOC-OLD",
        )
        self.repository.reserve_replacement_upload.return_value = (
            ReplacementUploadReservation(
                action=action,
                document_id="DOC-REPLACEMENT",
                event_id="replacement-event",
                correlation_id="replacement-correlation",
                status="published",
                should_upload=False,
            )
        )

        response = self.service.add_missing_document(
            claim_id=CLAIM_ID,
            document_type="damage_evidence",
            requested_action_id="ACT-REPLACE",
            idempotency_key="replacement-key-1",
            evidence=evidence(),
        )

        self.assertEqual(response.document_id, "DOC-REPLACEMENT")
        self.storage.upload_claim_document.assert_not_called()
        self.repository.add_document.assert_not_called()
        self.publisher.publish.assert_not_called()

    def test_arbitrary_damage_upload_without_action_is_rejected(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
        }

        with self.assertRaises(ClaimStorageValidationError):
            self.service.add_missing_document(
                claim_id=CLAIM_ID,
                document_type="damage_evidence",
                evidence=evidence(),
            )

        self.repository.reserve_replacement_upload.assert_not_called()
        self.storage.upload_claim_document.assert_not_called()

    def test_get_timeline_returns_oldest_first(self) -> None:
        self.repository.get_claim.return_value = {"claim_id": CLAIM_ID}
        self.repository.get_claim_events.return_value = [
            {
                "timestamp": NOW + timedelta(minutes=1),
                "action": "second",
                "actor": "firstnoticeai",
                "details": {},
            },
            {
                "timestamp": NOW,
                "action": "first",
                "actor": "claimant_api",
                "details": {},
            },
        ]

        timeline = self.service.get_timeline(CLAIM_ID)

        self.assertEqual([item.action for item in timeline], ["first", "second"])

    def test_get_claim_consolidates_internal_identity_requirements(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
            "intake_priority": "routine",
            "missing_documents": [
                {
                    "type": "vehicle_identity",
                    "reason": "Vehicle identity is required.",
                    "source_requirement": "always_required",
                },
                {
                    "type": "license_plate_photo",
                    "reason": "A clear plate photo is required.",
                    "source_requirement": "license_plate_photo",
                },
            ],
            "unusable_evidence": [],
            "updated_at": NOW,
        }
        self.repository.get_scheduled_appointment.return_value = None

        response = self.service.get_claim(CLAIM_ID)

        self.assertEqual(len(response.missing_documents), 2)
        self.assertEqual(len(response.requested_evidence), 1)
        request = response.requested_evidence[0]
        self.assertEqual(request.document_type, "license_plate_photo")
        self.assertEqual(
            set(request.satisfies_requirements),
            {"vehicle_identity", "license_plate_photo"},
        )
        self.assertEqual(
            response.action_display.title, "Vehicle identity not verified"
        )
        self.assertIn("readable license plate", response.action_display.explanation)

    def test_get_claim_exposes_missing_date_as_text_action_not_upload(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
            "intake_priority": "routine",
            "missing_documents": [{
                "type": "incident_date",
                "reason": "An incident date is required for intake routing.",
                "source_requirement": "always_required",
            }],
            "unusable_evidence": [],
            "requested_actions": [{
                "action_type": "enter_text",
                "action_id": "ACT-DATE",
                "review_id": "AUTONOMOUS-DATE",
                "field_name": "incident_date",
                "instruction": "Please provide the incident date to continue.",
            }],
            "updated_at": NOW,
        }
        self.repository.get_scheduled_appointment.return_value = None

        response = self.service.get_claim(CLAIM_ID)

        self.assertEqual(response.requested_evidence, [])
        self.assertEqual(response.requested_actions[0].action_type, "enter_text")
        self.assertEqual(response.requested_actions[0].field_name, "incident_date")

    def test_get_claim_exposes_async_voice_processing_result(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
            "missing_documents": [{"type": "incident_date"}],
            "requested_actions": [{
                "action_type": "enter_text",
                "action_id": "ACT-DATE",
                "review_id": "AUTONOMOUS-DATE",
                "field_name": "incident_date",
                "instruction": "Please provide the incident date.",
            }],
            "voice_correction_processing": {
                "document_id": "DOC-VOICE",
                "requested_action_id": "ACT-DATE",
                "status": "unusable",
                "message": "We could not use that recording. Please re-record your answer.",
            },
            "updated_at": NOW,
        }
        self.repository.get_scheduled_appointment.return_value = None

        response = self.service.get_claim(CLAIM_ID)

        self.assertEqual(
            response.voice_correction_processing["status"], "unusable"
        )
        self.assertEqual(
            response.voice_correction_processing["requested_action_id"],
            "ACT-DATE",
        )

    def test_get_claim_exposes_durable_manual_handling_state(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "manual_handling": True,
            "missing_documents": [],
            "unusable_evidence": [],
            "updated_at": NOW,
        }
        self.repository.get_scheduled_appointment.return_value = None

        response = self.service.get_claim(CLAIM_ID)

        self.assertTrue(response.manual_handling)
        self.assertEqual(response.requested_actions, [])

    def test_get_claim_exposes_one_upload_action_for_flow_3_remediation(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
            "intake_priority": "expedited",
            "missing_documents": [
                {
                    "type": "vehicle_identity",
                    "reason": "Vehicle identity is required.",
                    "source_requirement": "always_required",
                },
                {
                    "type": "license_plate_photo",
                    "reason": "A clear plate photo is required.",
                    "source_requirement": "license_plate_photo",
                },
            ],
            "unusable_evidence": [],
            "requested_actions": [
                {
                    "action_type": "upload_document",
                    "action_id": "ACT-FLOW-3",
                    "review_id": "AUTONOMOUS-FLOW-3",
                    "document_type": "damage_evidence",
                    "instruction": (
                        "Please upload a clear photo of the vehicle involved in "
                        "this incident showing the reported damage and a readable "
                        "license plate."
                    ),
                    "replaces_document_id": "DOC-FRONT",
                }
            ],
            "source_aware_conflicts": [{
                "fingerprint": "CFP-FLOW-3",
                "field": "vehicle_identity_and_damage_location",
                "selected_outlier_document_id": "DOC-FRONT",
                "assertions": [
                    {
                        "field": "damage_location",
                        "value": "rear",
                        "source_identity": "document:DOC-REPORT",
                        "filename": "police-report.pdf",
                        "document_id": "DOC-REPORT",
                        "document_type": "police_report",
                        "replaceable": False,
                    },
                    {
                        "field": "damage_location",
                        "value": "front",
                        "source_identity": "document:DOC-FRONT",
                        "filename": "front.jpg",
                        "document_id": "DOC-FRONT",
                        "document_type": "damage_evidence",
                        "replaceable": True,
                    },
                ],
            }],
            "updated_at": NOW,
        }
        self.repository.get_scheduled_appointment.return_value = None

        response = self.service.get_claim(CLAIM_ID)

        self.assertEqual(len(response.missing_documents), 2)
        self.assertEqual(response.requested_evidence, [])
        self.assertEqual(len(response.requested_actions), 1)
        self.assertEqual(
            response.requested_actions[0].action_id, "ACT-FLOW-3"
        )
        self.assertEqual(response.action_display.title, "Evidence doesn't match")
        display_json = response.action_display.model_dump_json()
        self.assertNotIn("DOC-", display_json)
        self.assertNotIn("ACT-", display_json)
        self.assertNotIn("AUTONOMOUS-", display_json)

    def test_get_claim_uses_document_request_provenance_for_flow_4_mismatch(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
            "intake_priority": "routine",
            "missing_documents": [{
                "type": "vehicle_identity",
                "reason": "Vehicle identity is required.",
                "source_requirement": "always_required",
            }],
            "unusable_evidence": [],
            "requested_actions": [{
                "action_type": "upload_document",
                "action_id": "ACT-RETRY",
                "review_id": "AUTONOMOUS-RETRY",
                "document_type": "damage_evidence",
                "instruction": "Please upload a clear vehicle photo with a readable plate.",
                "replaces_document_id": "DOC-WRONG-FOLLOWUP",
            }],
            "source_aware_conflicts": [{
                "fingerprint": "CFP-IDENTITY",
                "field": "vehicle_identity",
                "selected_outlier_document_id": "DOC-WRONG-FOLLOWUP",
                "assertions": [
                    {
                        "field": "vehicle_identity",
                        "value": "expected vehicle",
                        "source_identity": "document:DOC-REPORT",
                        "filename": "report.pdf",
                        "document_id": "DOC-REPORT",
                        "document_type": "police_report",
                        "replaceable": False,
                    },
                    {
                        "field": "vehicle_identity",
                        "value": "different vehicle",
                        "source_identity": "document:DOC-WRONG-FOLLOWUP",
                        "filename": "wrong.jpg",
                        "document_id": "DOC-WRONG-FOLLOWUP",
                        "document_type": "damage_evidence",
                        "replaceable": True,
                    },
                ],
            }],
            "updated_at": NOW,
        }
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-WRONG-FOLLOWUP",
                claim_id=CLAIM_ID,
                document_type="damage_evidence",
                filename="wrong.jpg",
                requested_action_id="ACT-INITIAL",
                status="validated",
                received_at=NOW,
            )
        ]
        self.repository.get_scheduled_appointment.return_value = None

        response = self.service.get_claim(CLAIM_ID)

        self.assertEqual(
            response.action_display.title,
            "This evidence doesn't match the vehicle in the claim.",
        )
        self.repository.get_documents.assert_called_once_with(CLAIM_ID)


class ClaimantApiEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MagicMock()
        self.client = TestClient(create_app(MagicMock(), self.service))

    def test_valid_claim_submission_returns_202(self) -> None:
        self.service.submit_claim.return_value = ClaimAcceptedResponse(
            claim_id=CLAIM_ID,
            status="new",
            event_id="event-123",
            message="Claim received and processing started.",
        )

        response = self.client.post(
            "/claims",
            headers={"X-Idempotency-Key": "request-key-123"},
            data={"incident_description": "Rear-ended at a stoplight"},
            files=[("files", ("accident.jpg", b"image", "image/jpeg"))],
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["claim_id"], CLAIM_ID)
        self.assertEqual(
            self.service.submit_claim.call_args.kwargs["idempotency_key"],
            "request-key-123",
        )

    def test_claim_submission_requires_idempotency_key(self) -> None:
        response = self.client.post(
            "/claims",
            data={"incident_description": "Rear-ended"},
            files=[("files", ("accident.jpg", b"image", "image/jpeg"))],
        )

        self.assertEqual(response.status_code, 422)
        self.service.submit_claim.assert_not_called()

    def test_voice_correction_endpoint_preserves_requested_action_association(self) -> None:
        human_review_service = MagicMock()
        human_review_service.submit_voice_incident_correction.return_value = (
            ClaimCorrectionAcceptedResponse(
                claim_id=CLAIM_ID,
                event_id="voice-event",
            )
        )
        client = TestClient(
            create_app(
                MagicMock(),
                self.service,
                human_review_service=human_review_service,
            )
        )

        response = client.post(
            f"/claims/{CLAIM_ID}/corrections/voice",
            headers={"X-Idempotency-Key": "voice-request-123"},
            data={"requested_action_id": "ACT-DATE"},
            files={"file": ("incident.webm", b"voice", "audio/webm")},
        )

        self.assertEqual(response.status_code, 202)
        call = human_review_service.submit_voice_incident_correction.call_args
        self.assertEqual(call.args[0], CLAIM_ID)
        self.assertEqual(call.kwargs["requested_action_id"], "ACT-DATE")
        self.assertEqual(call.kwargs["idempotency_key"], "voice-request-123")
        self.assertEqual(call.kwargs["content_type"], "audio/webm")

    def test_cors_allows_configured_frontend_origin(self) -> None:
        client = TestClient(
            create_app(MagicMock(), self.service, ["http://localhost:4200"])
        )

        response = client.options(
            "/claims",
            headers={
                "Origin": "http://localhost:4200",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Idempotency-Key",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://localhost:4200",
        )
        self.assertNotIn("access-control-allow-credentials", response.headers)

    def test_cors_rejects_unconfigured_origin(self) -> None:
        client = TestClient(
            create_app(MagicMock(), self.service, ["https://demo.example"])
        )
        response = client.options(
            "/claims",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_get_claim_returns_compact_state(self) -> None:
        self.service.get_claim.return_value = ClaimSummaryResponse(
            claim_id=CLAIM_ID,
            status="awaiting_documents",
            intake_priority="routine",
            missing_documents=[{"type": "license_plate_photo"}],
            updated_at=NOW,
        )

        response = self.client.get(f"/claims/{CLAIM_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "awaiting_documents")

    def test_get_timeline_returns_events(self) -> None:
        self.service.get_timeline.return_value = [
            ClaimTimelineEvent(
                timestamp=NOW,
                action="claim_submission_received",
                actor="claimant_api",
            )
        ]

        response = self.client.get(f"/claims/{CLAIM_ID}/events")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["action"], "claim_submission_received")

    def test_missing_document_upload_returns_202(self) -> None:
        self.service.add_missing_document.return_value = DocumentAcceptedResponse(
            claim_id=CLAIM_ID,
            document_id=DOCUMENT_ID,
            status="received",
            event_id="event-document",
        )

        response = self.client.post(
            f"/claims/{CLAIM_ID}/documents",
            data={"document_type": "license_plate_photo"},
            files={"file": ("plate.jpg", b"image", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["document_id"], DOCUMENT_ID)

    def test_batch_evidence_upload_preserves_action_to_file_association(self) -> None:
        self.service.add_missing_document.side_effect = [
            DocumentAcceptedResponse(
                claim_id=CLAIM_ID, document_id="DOC-POLICY",
                status="received", event_id="event-policy",
            ),
            DocumentAcceptedResponse(
                claim_id=CLAIM_ID, document_id="DOC-REPORT",
                status="received", event_id="event-report",
            ),
        ]

        response = self.client.post(
            f"/claims/{CLAIM_ID}/documents/batch",
            files=[
                ("document_types", (None, "policy_document")),
                ("document_types", (None, "police_report")),
                ("requested_action_ids", (None, "ACT-POLICY")),
                ("requested_action_ids", (None, "ACT-REPORT")),
                ("idempotency_keys", (None, "policy-upload-key")),
                ("idempotency_keys", (None, "report-upload-key")),
                ("files", ("policy.pdf", b"policy", "application/pdf")),
                ("files", ("report.pdf", b"report", "application/pdf")),
            ],
        )

        self.assertEqual(response.status_code, 202)
        calls = self.service.add_missing_document.call_args_list
        self.assertEqual(calls[0].kwargs["requested_action_id"], "ACT-POLICY")
        self.assertEqual(calls[1].kwargs["requested_action_id"], "ACT-REPORT")
        self.assertEqual(calls[0].kwargs["evidence"].filename, "policy.pdf")
        self.assertEqual(calls[1].kwargs["evidence"].filename, "report.pdf")

    def test_replacement_upload_contract_forwards_action_and_idempotency_only(self) -> None:
        self.service.add_missing_document.return_value = DocumentAcceptedResponse(
            claim_id=CLAIM_ID,
            document_id=DOCUMENT_ID,
            status="received",
            event_id="event-document",
        )

        response = self.client.post(
            f"/claims/{CLAIM_ID}/documents",
            data={
                "document_type": "damage_evidence",
                "requested_action_id": "ACT-REPLACE",
                "replaces_document_id": "DOC-BROWSER-CHOICE",
            },
            headers={"X-Idempotency-Key": "replacement-request-1"},
            files={"file": ("damage.jpg", b"image", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 202)
        kwargs = self.service.add_missing_document.call_args.kwargs
        self.assertEqual(kwargs["requested_action_id"], "ACT-REPLACE")
        self.assertEqual(kwargs["idempotency_key"], "replacement-request-1")
        self.assertNotIn("replaces_document_id", kwargs)

    def test_missing_document_unknown_claim_returns_404(self) -> None:
        self.service.add_missing_document.side_effect = ClaimNotFoundError("missing")

        response = self.client.post(
            f"/claims/{CLAIM_ID}/documents",
            data={"document_type": "license_plate_photo"},
            files={"file": ("plate.jpg", b"image", "image/jpeg")},
        )

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
