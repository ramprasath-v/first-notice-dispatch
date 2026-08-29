import unittest
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock

from app.models.claim_document import ClaimDocument
from app.services.claim_storage_service import StoredClaimObject, ValidatedUpload
from app.services.human_review_service import (
    HumanReviewConflictError,
    HumanReviewService,
    HumanReviewSettings,
)
from app.services.voice_incident_extraction_service import (
    GeminiVoiceIncidentExtractor,
    VoiceIncidentExtractionResult,
)


CLAIM_ID = "CLM-VOICE001"
ACTION_ID = "ACT-INCIDENT-DATE"
NOW = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)


def awaiting_date_claim(**updates):
    claim = {
        "claim_id": CLAIM_ID,
        "status": "awaiting_documents",
        "incident_date": None,
        "vehicle_identity": "2014 Toyota Corolla",
        "requested_actions": [{
            "action_type": "enter_text",
            "action_id": ACTION_ID,
            "review_id": "AUTONOMOUS-DATE",
            "field_name": "incident_date",
            "instruction": "Please provide the incident date to continue.",
        }],
    }
    claim.update(updates)
    return claim


def awaiting_incident_claim(*, field_name: str, missing: list[str]):
    return awaiting_date_claim(
        incident_date=(None if "incident_date" in missing else "2026-08-23"),
        incident_description="",
        missing_documents=[{"type": item} for item in missing],
        requested_actions=[{
            "action_type": "enter_text",
            "action_id": ACTION_ID,
            "review_id": "AUTONOMOUS-INCIDENT",
            "field_name": field_name,
            "instruction": "Tell us when and what happened.",
        }],
    )


class VoiceIncidentRemediationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.repository.get_claim.return_value = awaiting_date_claim()
        self.repository.get_documents.return_value = []
        self.repository.get_document.return_value = None
        self.publisher = MagicMock()
        self.storage = MagicMock()
        self.storage.validate_upload.return_value = ValidatedUpload(
            filename="incident.webm",
            content_type="audio/webm",
            size_bytes=12,
        )
        self.storage.upload_claim_document.return_value = StoredClaimObject(
            filename="incident.webm",
            content_type="audio/webm",
            size_bytes=12,
            bucket="claim-bucket",
            object_name="claims/CLM-VOICE001/documents/DOC/audio.webm",
            gs_uri="gs://claim-bucket/claims/CLM-VOICE001/documents/DOC/audio.webm",
            document_id="DOC-VOICE",
        )
        self.extractor = MagicMock()
        self.service = HumanReviewService(
            repository=self.repository,
            publisher=self.publisher,
            settings=HumanReviewSettings("https://firstnotice-web.example", 60),
            storage_service=self.storage,
            voice_incident_extractor=self.extractor,
        )

    def submit(self):
        return self.service.submit_voice_incident_correction(
            CLAIM_ID,
            requested_action_id=ACTION_ID,
            idempotency_key="voice-request-123",
            file_obj=BytesIO(b"voice-bytes"),
            filename="incident.webm",
            content_type="audio/webm",
        )

    def submit_and_process(self):
        accepted = self.submit()
        document = self.repository.add_document.call_args.args[0]
        self.repository.get_document.return_value = document
        self.publisher.reset_mock()
        processed = self.service.process_voice_incident_document(
            CLAIM_ID, document.document_id
        )
        return accepted, processed

    def test_upload_is_accepted_before_voice_extraction(self) -> None:
        accepted = self.submit()

        self.assertEqual(accepted.status, "received")
        self.extractor.extract.assert_not_called()
        event = self.publisher.publish.call_args.args[0]
        self.assertEqual(event.event_type, "claim.document.received")
        self.repository.mark_voice_correction_processing.assert_called_once()

    def test_duplicate_upload_reuses_deterministic_document_and_event(self) -> None:
        first = self.submit()
        document = self.repository.add_document.call_args.args[0]
        self.repository.get_document.return_value = document

        second = self.submit()

        self.assertEqual(first.event_id, second.event_id)
        self.storage.upload_claim_document.assert_called_once()
        self.repository.add_document.assert_called_once()
        published = [call.args[0] for call in self.publisher.publish.call_args_list]
        self.assertEqual([event.event_id for event in published], [first.event_id] * 2)

    def test_valid_date_without_injury_reuses_correction_event(self) -> None:
        self.extractor.extract.return_value = VoiceIncidentExtractionResult(
            incident_date="2026-08-24",
            incident_time="18:00",
            incident_description=None,
            injury_mentioned=False,
        )

        self.submit_and_process()

        saved = self.repository.add_document.call_args.args[0]
        self.assertEqual(saved.source_type, "claimant_voice")
        self.assertEqual(saved.requested_action_id, ACTION_ID)
        self.assertEqual(saved.document_type, "voice_note")
        saved_correction = (
            self.repository.save_claim_voice_incident_correction.call_args.kwargs
        )
        self.assertEqual(saved_correction["requested_field"], "incident_date")
        self.assertEqual(saved_correction["incident_date"], "2026-08-24")
        self.assertIsNone(saved_correction["incident_description"])
        event = self.publisher.publish.call_args.args[0]
        self.assertEqual(event.event_type, "claim.correction.received")
        self.assertEqual(event.payload.source_type, "claimant_voice")
        self.assertFalse(event.payload.injury_mentioned)
        self.assertEqual(
            self.repository.get_claim.return_value["vehicle_identity"],
            "2014 Toyota Corolla",
        )

    def test_valid_date_with_injury_carries_deterministic_safety_signal(self) -> None:
        self.extractor.extract.return_value = VoiceIncidentExtractionResult(
            incident_date="2026-08-24",
            incident_time="18:00",
            incident_description=None,
            injury_mentioned=True,
            injury_description="neck pain later that evening",
        )

        self.submit_and_process()

        self.repository.record_claimant_voice_injury_signal.assert_called_once()
        event = self.publisher.publish.call_args.args[0]
        self.assertTrue(event.payload.injury_mentioned)
        self.assertEqual(
            event.payload.injury_description, "neck pain later that evening"
        )

    def test_voice_without_date_stays_paused(self) -> None:
        self.extractor.extract.return_value = VoiceIncidentExtractionResult(
            incident_date=None,
            incident_time=None,
            injury_mentioned=False,
        )

        _, processed = self.submit_and_process()

        self.assertEqual(processed["action"], "voice_correction_unusable")
        self.repository.mark_document_unusable.assert_called_once()
        self.repository.save_claim_voice_incident_correction.assert_not_called()
        self.publisher.publish.assert_not_called()

    def test_future_date_is_rejected_and_claim_stays_paused(self) -> None:
        self.extractor.extract.return_value = VoiceIncidentExtractionResult(
            incident_date="2099-01-01",
            incident_time=None,
            injury_mentioned=False,
        )

        _, processed = self.submit_and_process()

        self.assertEqual(processed["action"], "voice_correction_unusable")
        self.repository.mark_document_unusable.assert_called_once()
        self.repository.save_claim_voice_incident_correction.assert_not_called()

    def test_existing_stronger_date_is_not_overwritten(self) -> None:
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-REPORT",
                claim_id=CLAIM_ID,
                document_type="police_report",
                filename="report.pdf",
                status="validated",
                evidence_facts={"incident_date": "2026-08-23"},
                received_at=NOW,
            )
        ]

        with self.assertRaisesRegex(HumanReviewConflictError, "cannot overwrite"):
            self.submit()

        self.storage.upload_claim_document.assert_not_called()
        self.extractor.extract.assert_not_called()
        self.repository.save_claim_voice_incident_correction.assert_not_called()

    def test_date_and_context_are_accepted_once_with_injury(self) -> None:
        self.repository.get_claim.return_value = awaiting_incident_claim(
            field_name="incident_information",
            missing=["incident_date", "incident_description"],
        )
        self.extractor.extract.return_value = VoiceIncidentExtractionResult(
            incident_date="2026-08-24",
            incident_time="13:00",
            incident_description="I was rear-ended while stopped at a light.",
            injury_mentioned=True,
            injury_description="my neck started hurting later",
        )

        self.submit_and_process()

        saved = self.repository.save_claim_voice_incident_correction.call_args.kwargs
        self.assertEqual(saved["requested_field"], "incident_information")
        self.assertEqual(saved["incident_date"], "2026-08-24")
        self.assertEqual(
            saved["incident_description"],
            "I was rear-ended while stopped at a light.",
        )
        event = self.publisher.publish.call_args.args[0]
        self.assertEqual(event.payload.field_name, "incident_information")
        self.assertTrue(event.payload.injury_mentioned)

    def test_context_only_gap_accepts_voice_without_date(self) -> None:
        self.repository.get_claim.return_value = awaiting_incident_claim(
            field_name="incident_description",
            missing=["incident_description"],
        )
        self.extractor.extract.return_value = VoiceIncidentExtractionResult(
            incident_date=None,
            incident_time=None,
            incident_description="I was rear-ended at a traffic light.",
            injury_mentioned=False,
        )

        self.submit_and_process()

        saved = self.repository.save_claim_voice_incident_correction.call_args.kwargs
        self.assertIsNone(saved["incident_date"])
        self.assertEqual(
            saved["incident_description"],
            "I was rear-ended at a traffic light.",
        )

    def test_missing_required_context_keeps_voice_action_active(self) -> None:
        self.repository.get_claim.return_value = awaiting_incident_claim(
            field_name="incident_information",
            missing=["incident_date", "incident_description"],
        )
        self.extractor.extract.return_value = VoiceIncidentExtractionResult(
            incident_date="2026-08-24",
            incident_time=None,
            incident_description=None,
            injury_mentioned=False,
        )

        _, processed = self.submit_and_process()

        self.assertEqual(processed["action"], "voice_correction_unusable")
        self.repository.mark_document_unusable.assert_called_once()
        self.repository.save_claim_voice_incident_correction.assert_not_called()
        self.publisher.publish.assert_not_called()

    def test_wrong_requested_action_is_rejected(self) -> None:
        with self.assertRaisesRegex(HumanReviewConflictError, "not currently requested"):
            self.service.submit_voice_incident_correction(
                CLAIM_ID,
                requested_action_id="ACT-OTHER",
                idempotency_key="voice-request-123",
                file_obj=BytesIO(b"voice-bytes"),
                filename="incident.webm",
                content_type="audio/webm",
            )
        self.extractor.extract.assert_not_called()


class VoiceIncidentExtractionSchemaTests(unittest.TestCase):
    def test_real_generation_config_uses_strict_structured_schema(self) -> None:
        client = MagicMock()
        client.models.generate_content.return_value.text = (
            '{"incident_date":"2026-08-24","incident_time":"18:00",'
            '"injury_mentioned":true,"injury_description":"neck pain"}'
        )
        extractor = GeminiVoiceIncidentExtractor(client, "configured-model")

        result = extractor.extract(
            "gs://claim-bucket/voice.webm",
            mime_type="audio/webm",
            filename="voice.webm",
        )

        self.assertTrue(result.injury_mentioned)
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIs(config.response_schema, VoiceIncidentExtractionResult)
        self.assertEqual(config.response_mime_type, "application/json")


if __name__ == "__main__":
    unittest.main()
