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

    def test_valid_date_without_injury_reuses_correction_event(self) -> None:
        self.extractor.extract.return_value = VoiceIncidentExtractionResult(
            incident_date="2026-08-24",
            incident_time="18:00",
            injury_mentioned=False,
        )

        self.submit()

        saved = self.repository.add_document.call_args.args[0]
        self.assertEqual(saved.source_type, "claimant_voice")
        self.assertEqual(saved.requested_action_id, ACTION_ID)
        self.assertEqual(saved.document_type, "voice_note")
        self.assertEqual(
            self.repository.save_claim_correction.call_args.kwargs["field_name"],
            "incident_date",
        )
        self.assertEqual(
            self.repository.save_claim_correction.call_args.kwargs["value"],
            "2026-08-24",
        )
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
            injury_mentioned=True,
            injury_description="neck pain later that evening",
        )

        self.submit()

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

        with self.assertRaisesRegex(HumanReviewConflictError, "could not determine"):
            self.submit()

        self.repository.mark_document_unusable.assert_called_once()
        self.repository.save_claim_correction.assert_not_called()
        self.publisher.publish.assert_not_called()

    def test_future_date_is_rejected_and_claim_stays_paused(self) -> None:
        self.extractor.extract.return_value = VoiceIncidentExtractionResult(
            incident_date="2099-01-01",
            incident_time=None,
            injury_mentioned=False,
        )

        with self.assertRaisesRegex(HumanReviewConflictError, "not in the future"):
            self.submit()

        self.repository.mark_document_unusable.assert_called_once()
        self.repository.save_claim_correction.assert_not_called()

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
        self.repository.save_claim_correction.assert_not_called()

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
