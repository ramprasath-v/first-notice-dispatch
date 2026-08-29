import unittest
from unittest.mock import MagicMock

from app.models.intake_result import (
    EvidenceArtifactClassification,
    ImageEvidenceCapabilities,
    IntakeResult,
)
from app.models.review_result import (
    ClaimEvidenceMetadata,
    OperationalIndicators,
    ReviewResult,
    UploadedEvidence,
)
from app.models.requested_action import UploadDocumentRequestedAction
from app.services.claim_review_service import ClaimReviewService
from app.services.intake_extraction_service import IntakeExtractionService


class IntakeClaimTypeNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock(name="gemini_client")
        self.service = IntakeExtractionService(self.client, "configured-model-id")

    def extract(self, result: IntakeResult) -> IntakeResult:
        self.client.models.generate_content.return_value.text = result.model_dump_json()
        return self.service.extract(
            ["gs://claim-bucket/policy.pdf", "gs://claim-bucket/vehicle.jpg"],
        )

    def unknown_collision_result(self) -> IntakeResult:
        return IntakeResult(
            claim_type="unknown",
            damage_type="Collision damage to the vehicle",
            parts_affected=["rear bumper"],
            incident_summary="The vehicle was involved in a collision.",
            policy_number="POL-123",
            incident_date=None,
            vehicle_drivable=True,
            evidence_artifact_classifications=[
                EvidenceArtifactClassification(
                    source="policy.pdf", document_type="policy_document"
                ),
                EvidenceArtifactClassification(
                    source="vehicle.jpg", document_type="damage_evidence"
                ),
            ],
            image_evidence_capabilities=[
                ImageEvidenceCapabilities(
                    source="vehicle.jpg",
                    supported_capabilities=["damage_evidence"],
                    quality_observations=["No readable vehicle identifier is visible."],
                )
            ],
        )

    def test_unknown_grounded_collision_normalizes_without_plate_or_date(self) -> None:
        result = self.extract(self.unknown_collision_result())

        self.assertEqual(result.claim_type, "auto_collision")
        self.assertIsNone(result.incident_date)
        self.assertNotIn(
            "vehicle_identity",
            result.image_evidence_capabilities[0].supported_capabilities,
        )

    def test_normalized_collision_reaches_vehicle_first_remediation(self) -> None:
        intake = self.extract(self.unknown_collision_result())
        review_client = MagicMock(name="review_client")
        review_client.models.generate_content.return_value.text = ReviewResult(
            intake_complete=False,
            intake_priority="routine",
            priority_reason="Evidence gaps remain.",
            confidence=0.8,
            inspection_required=True,
            requires_human_review=False,
            operational_indicators=OperationalIndicators(),
        ).model_dump_json()
        metadata = ClaimEvidenceMetadata(
            vehicle_identity_clear=False,
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="vehicle.jpg",
                    document_id="DOC-VEHICLE",
                    document_type="damage_evidence",
                    status="validated",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="policy_document",
                    filename="policy.pdf",
                    document_id="DOC-POLICY",
                    document_type="policy_document",
                    status="received",
                ),
            ],
        )

        review = ClaimReviewService(review_client, "configured-model-id").review(
            intake, metadata
        )

        self.assertEqual(len(review.requested_actions), 1)
        action = review.requested_actions[0]
        self.assertIsInstance(action, UploadDocumentRequestedAction)
        self.assertEqual(action.document_type, "license_plate_photo")
        self.assertIn("incident_date", [item.type for item in review.missing_documents])

    def test_genuinely_unsupported_claim_type_is_not_normalized(self) -> None:
        unsupported = self.unknown_collision_result().model_copy(
            update={
                "claim_type": "theft",
                "incident_summary": "The vehicle was reported stolen.",
                "damage_type": "",
            }
        )

        result = self.extract(unsupported)

        self.assertEqual(result.claim_type, "theft")


if __name__ == "__main__":
    unittest.main()
