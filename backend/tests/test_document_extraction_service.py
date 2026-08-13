import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.models.claim_document import ClaimDocument, DocumentExtractionResult
from app.models.intake_result import EvidenceArtifactFacts
from app.services.document_extraction_service import (
    GeminiDocumentExtractor,
    UnsupportedResumeDocumentTypeError,
)


class GeminiDocumentExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock()
        self.extractor = GeminiDocumentExtractor(self.client, "configured-model")

    def test_all_requestable_document_types_use_structured_fact_extraction(self) -> None:
        cases = {
            "policy_document": EvidenceArtifactFacts(
                source="policy.pdf", policy_number="POL-123", vehicle_make="Toyota"
            ),
            "police_report": EvidenceArtifactFacts(
                source="report.pdf", incident_date="2026-08-05", vehicle_make="Toyota"
            ),
            "damage_evidence": EvidenceArtifactFacts(
                source="damage.jpg", license_plate="7ABX123", damage_location="rear"
            ),
        }
        for index, (document_type, facts) in enumerate(cases.items()):
            with self.subTest(document_type=document_type):
                self.client.models.generate_content.return_value.text = (
                    DocumentExtractionResult(
                        usable=True,
                        reason="The evidence is readable.",
                        evidence_facts=facts,
                    ).model_dump_json()
                )
                result = self.extractor.extract(
                    ClaimDocument(
                        document_id=f"DOC-{index}",
                        claim_id="CLM-A1B2C3D4",
                        document_type=document_type,
                        filename=facts.source,
                        storage_path=f"gs://evidence/{facts.source}",
                        content_type=(
                            "application/pdf"
                            if facts.source.endswith(".pdf")
                            else "image/jpeg"
                        ),
                        received_at=datetime.now(timezone.utc),
                    ),
                    document_type,
                )

                self.assertEqual(result.evidence_facts, facts)
                self.assertEqual(result.satisfies_requirement, document_type)

        self.assertEqual(self.client.models.generate_content.call_count, 3)

    def test_unsupported_type_fails_before_provider_request(self) -> None:
        value = ClaimDocument(
            document_id="DOC-UNKNOWN",
            claim_id="CLM-A1B2C3D4",
            document_type="unknown_artifact",
            filename="unknown.bin",
            storage_path="gs://evidence/unknown.bin",
            received_at=datetime.now(timezone.utc),
        )

        with self.assertRaisesRegex(
            UnsupportedResumeDocumentTypeError,
            "Unsupported resume document type: unknown_artifact",
        ):
            self.extractor.extract(value, "unknown_artifact")

        self.client.models.generate_content.assert_not_called()

    def test_medical_document_is_received_without_content_analysis(self) -> None:
        document = ClaimDocument(
            document_id="DOC-MEDICAL",
            claim_id="CLM-A1B2C3D4",
            document_type="medical_document",
            filename="medical-record.pdf",
            storage_path="gs://evidence/medical-record.pdf",
            content_type="application/pdf",
            received_at=datetime.now(timezone.utc),
        )

        result = self.extractor.extract(document, "medical_document")

        self.assertTrue(result.usable)
        self.assertEqual(result.satisfies_requirement, "medical_document")
        self.assertEqual(result.evidence_findings, [])
        self.assertIsNone(result.evidence_facts)
        self.assertEqual(result.conflicts, [])
        self.client.models.generate_content.assert_not_called()


if __name__ == "__main__":
    unittest.main()
