import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from pydantic import ValidationError

from app.domain.claim_status import (
    ClaimStatus,
    InvalidClaimStatusTransition,
    review_target_status,
    validate_claim_status_transition,
)
from app.models.claim_document import ClaimDocument, DocumentExtractionResult
from app.models.intake_result import ImageEvidenceCapabilities, IntakeResult
from app.models.review_result import (
    ClaimEvidenceMetadata,
    EvidenceConflict,
    MissingEvidence,
    OperationalIndicators,
    ReviewResult,
    UnusableEvidence,
    UploadedEvidence,
)
from app.services.claim_review_service import ClaimReviewService
from app.services.document_extraction_service import GeminiDocumentExtractor
from app.tools.adk_workflow_tools import build_initial_review_metadata


def intake_result(*, vehicle_drivable: bool | None = True) -> IntakeResult:
    return IntakeResult(
        claim_type="auto_collision",
        damage_type="Front bumper damage",
        parts_affected=["front bumper"],
        incident_summary="The vehicle was involved in a collision.",
        policy_number="POL-12345",
        incident_date="2026-08-01",
        vehicle_drivable=vehicle_drivable,
        evidence_findings=[],
        uncertainties=[],
    )


def complete_metadata(**overrides) -> ClaimEvidenceMetadata:
    values = {
        "uploaded_evidence": [
            UploadedEvidence(
                evidence_type="damage_evidence",
                filename="accident-photo.jpg",
                usable=True,
            ),
            UploadedEvidence(
                evidence_type="vehicle_identity",
                filename="police-report.pdf",
                usable=True,
            ),
        ]
    }
    values.update(overrides)
    return ClaimEvidenceMetadata(**values)


def ai_review(**overrides) -> ReviewResult:
    values = {
        "intake_complete": True,
        "intake_priority": "routine",
        "priority_reason": "No urgent indicator.",
        "confidence": 0.9,
        "inspection_required": True,
        "missing_documents": [],
        "unusable_evidence": [],
        "conflicts": [],
        "requires_human_review": False,
        "human_review_reason": None,
        "operational_indicators": OperationalIndicators(),
    }
    values.update(overrides)
    return ReviewResult(**values)


class ClaimReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock(name="gemini_client")
        self.service = ClaimReviewService(self.client, "configured-model-id")

    def run_review(
        self,
        *,
        intake: IntakeResult | None = None,
        metadata: ClaimEvidenceMetadata | None = None,
        model_review: ReviewResult | None = None,
    ) -> ReviewResult:
        self.client.models.generate_content.return_value.text = (
            model_review or ai_review()
        ).model_dump_json()
        return self.service.review(
            intake or intake_result(), metadata or complete_metadata()
        )

    def test_complete_intake_routes_to_inspection_pending(self) -> None:
        review = self.run_review()

        self.assertTrue(review.intake_complete)
        self.assertEqual(review.intake_priority, "routine")
        self.assertEqual(
            review_target_status(review), ClaimStatus.INSPECTION_PENDING
        )
        call = self.client.models.generate_content.call_args
        self.assertEqual(call.kwargs["model"], "configured-model-id")
        self.assertEqual(call.kwargs["config"].temperature, 0.1)
        self.assertIs(call.kwargs["config"].response_schema, ReviewResult)

    def test_missing_license_plate_routes_to_awaiting_documents(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="accident-photo.jpg",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="vehicle_identity",
                    filename="police-report.pdf",
                    usable=True,
                ),
            ],
            vehicle_identity_clear=False,
        )

        review = self.run_review(metadata=metadata)

        self.assertIn(
            "license_plate_photo", [item.type for item in review.missing_documents]
        )
        self.assertFalse(review.intake_complete)
        self.assertEqual(review.intake_priority, "routine")
        self.assertFalse(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )

    def test_missing_vehicle_identity_only_routes_to_awaiting_documents(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="accident-photo.jpg",
                    usable=True,
                )
            ],
            vehicle_identity_clear=None,
        )

        review = self.run_review(metadata=metadata)

        self.assertEqual(
            [item.type for item in review.missing_documents], ["vehicle_identity"]
        )
        self.assertEqual(review.intake_priority, "routine")
        self.assertFalse(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )

    def test_both_identity_gaps_ignore_workflow_pseudo_conflict(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="accident-photo.jpg",
                    usable=True,
                )
            ],
            vehicle_identity_clear=False,
        )
        model_review = ai_review(
            intake_complete=False,
            operational_indicators=OperationalIndicators(significant_damage=True),
            conflicts=[
                EvidenceConflict(
                    field="vehicle_identity",
                    values=["Toyota Corolla", "Vehicle identity is missing"],
                    sources=["accident-photo.jpg", "checklist / metadata"],
                    reason="Workflow metadata marks vehicle identity as missing.",
                )
            ],
        )

        review = self.run_review(metadata=metadata, model_review=model_review)

        self.assertEqual(
            {item.type for item in review.missing_documents},
            {"vehicle_identity", "license_plate_photo"},
        )
        self.assertEqual(review.conflicts, [])
        self.assertEqual(review.intake_priority, "expedited")
        self.assertFalse(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )

    def test_blurry_license_plate_is_unusable_and_awaiting_documents(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="accident-photo.jpg",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="license_plate_photo",
                    filename="plate.jpg",
                    usable=False,
                    quality_observations=["License plate is too dark and blurry."],
                ),
            ],
            vehicle_identity_clear=False,
        )
        model_review = ai_review(
            intake_complete=False,
            unusable_evidence=[
                UnusableEvidence(
                    evidence_type="license_plate_photo",
                    reason="The license plate cannot be verified.",
                    suggested_action="Upload a clearer photo.",
                )
            ],
        )

        review = self.run_review(metadata=metadata, model_review=model_review)

        self.assertEqual(review.unusable_evidence[0].evidence_type, "license_plate_photo")
        self.assertFalse(review.intake_complete)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )

    def test_clear_damage_without_plate_routes_to_awaiting_both_identity_documents(
        self,
    ) -> None:
        uncertain_intake = intake_result().model_copy(
            update={
                "uncertainties": [
                    "Vehicle identity is unavailable.",
                    "The license plate is not visible.",
                    "The vehicle identifier cannot be confirmed.",
                ]
            }
        )
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="vehicle-photo.jpg",
                    usable=True,
                    quality_observations=[
                        "Clear close-up damage evidence; no plate is visible."
                    ],
                )
            ],
            vehicle_identity_clear=False,
        )

        review = self.run_review(intake=uncertain_intake, metadata=metadata)

        self.assertEqual(
            {item.type for item in review.missing_documents},
            {"vehicle_identity", "license_plate_photo"},
        )
        self.assertEqual(review.intake_priority, "routine")
        self.assertFalse(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )

    def test_blurry_plate_with_high_uncertainty_remains_awaiting_documents(
        self,
    ) -> None:
        uncertain_intake = intake_result().model_copy(
            update={"uncertainties": ["Plate unreadable", "VIN absent", "ID unknown"]}
        )
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="damage.jpg",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="license_plate_photo",
                    filename="plate.jpg",
                    usable=False,
                    quality_observations=["The plate is too blurry to read."],
                ),
            ],
            vehicle_identity_clear=False,
        )

        review = self.run_review(intake=uncertain_intake, metadata=metadata)

        self.assertTrue(review.unusable_evidence)
        self.assertFalse(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )

    def test_low_confidence_from_missing_routine_evidence_is_not_human_review(
        self,
    ) -> None:
        uncertain_intake = intake_result().model_copy(
            update={"uncertainties": ["Plate absent", "VIN absent", "ID unknown"]}
        )
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="damage.jpg",
                    usable=True,
                )
            ],
            vehicle_identity_clear=False,
        )
        model_review = ai_review(
            intake_complete=False,
            confidence=0.1,
            operational_indicators=OperationalIndicators(
                high_operational_uncertainty=True
            ),
        )

        review = self.run_review(
            intake=uncertain_intake,
            metadata=metadata,
            model_review=model_review,
        )

        self.assertEqual(review.confidence, 0.1)
        self.assertFalse(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )

    def test_no_vehicle_identity_with_high_uncertainty_is_awaiting_documents(
        self,
    ) -> None:
        uncertain_intake = intake_result().model_copy(
            update={"uncertainties": ["Identity absent", "VIN unknown", "Make unknown"]}
        )
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="damage.jpg",
                    usable=True,
                )
            ],
            vehicle_identity_clear=None,
        )

        review = self.run_review(intake=uncertain_intake, metadata=metadata)

        self.assertEqual(
            [item.type for item in review.missing_documents], ["vehicle_identity"]
        )
        self.assertFalse(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )

    def test_low_confidence_with_major_conflict_still_requires_human_review(
        self,
    ) -> None:
        uncertain_intake = intake_result().model_copy(
            update={"uncertainties": ["Plate absent", "VIN absent", "ID unknown"]}
        )
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="damage.jpg",
                    usable=True,
                )
            ],
            vehicle_identity_clear=False,
            known_conflicts=[
                EvidenceConflict(
                    field="incident_date",
                    values=["2026-08-01", "2026-08-02"],
                    sources=["police-report.pdf", "incident description"],
                    reason="Submitted sources report different incident dates.",
                )
            ],
        )

        review = self.run_review(
            intake=uncertain_intake,
            metadata=metadata,
            model_review=ai_review(confidence=0.1),
        )

        self.assertTrue(review.requires_human_review)
        self.assertEqual(review.intake_priority, "urgent_human_review")
        self.assertEqual(
            review_target_status(review), ClaimStatus.HUMAN_REVIEW_REQUIRED
        )

    def test_vehicle_not_drivable_is_expedited(self) -> None:
        review = self.run_review(intake=intake_result(vehicle_drivable=False))

        self.assertEqual(review.intake_priority, "expedited")
        self.assertFalse(review.requires_human_review)

    def test_injury_indicator_requires_urgent_human_review(self) -> None:
        review = self.run_review(
            metadata=complete_metadata(injury_mentioned=True)
        )

        self.assertEqual(review.intake_priority, "urgent_human_review")
        self.assertTrue(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.HUMAN_REVIEW_REQUIRED
        )

    def test_injury_indicator_overrides_resolvable_missing_evidence(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="damage.jpg",
                    usable=True,
                )
            ],
            vehicle_identity_clear=False,
            injury_mentioned=True,
        )

        review = self.run_review(metadata=metadata)

        self.assertTrue(review.missing_documents)
        self.assertTrue(review.requires_human_review)
        self.assertEqual(review.intake_priority, "urgent_human_review")
        self.assertEqual(
            review_target_status(review), ClaimStatus.HUMAN_REVIEW_REQUIRED
        )

    def test_significant_safety_concern_requires_human_review(self) -> None:
        review = self.run_review(metadata=complete_metadata(safety_concern=True))

        self.assertEqual(review.intake_priority, "urgent_human_review")
        self.assertTrue(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.HUMAN_REVIEW_REQUIRED
        )

    def test_consequential_uncertainty_after_complete_evidence_requires_human_review(
        self,
    ) -> None:
        uncertain_intake = intake_result().model_copy(
            update={
                "uncertainties": [
                    "Damage extent remains ambiguous.",
                    "Structural involvement cannot be ruled out.",
                    "The safe inspection route cannot be selected.",
                ]
            }
        )

        review = self.run_review(intake=uncertain_intake)

        self.assertEqual(review.missing_documents, [])
        self.assertEqual(review.unusable_evidence, [])
        self.assertEqual(review.intake_priority, "urgent_human_review")
        self.assertTrue(review.requires_human_review)
        self.assertEqual(
            review.human_review_reason,
            "High uncertainty affects the next operational routing step.",
        )

    def test_explicit_no_injury_overrides_unsupported_ai_indicator(self) -> None:
        no_injury_intake = intake_result().model_copy(
            update={"incident_summary": "The driver reported no injuries."}
        )
        model_review = ai_review(
            operational_indicators=OperationalIndicators(possible_injury=True)
        )

        review = self.run_review(
            intake=no_injury_intake,
            model_review=model_review,
        )

        self.assertEqual(review.intake_priority, "routine")
        self.assertFalse(review.requires_human_review)

    def test_minor_conflict_does_not_block_operational_completeness(self) -> None:
        model_review = ai_review(
            intake_complete=False,
            conflicts=[
                EvidenceConflict(
                    field="damaged_parts",
                    values=["bumper", "bumper and tail light"],
                    sources=["police-report.pdf", "accident-photo.jpg"],
                    reason="One source lists more visible damage.",
                )
            ],
        )

        review = self.run_review(model_review=model_review)

        self.assertTrue(review.intake_complete)
        self.assertFalse(review.requires_human_review)

    def test_major_conflicting_evidence_requires_human_review(self) -> None:
        model_review = ai_review(
            intake_complete=False,
            conflicts=[
                EvidenceConflict(
                    field="incident_date",
                    values=["2026-08-01", "2026-08-02"],
                    sources=["police-report.pdf", "incident description"],
                    reason="The submitted sources contain different dates.",
                )
            ],
        )

        review = self.run_review(model_review=model_review)

        self.assertEqual(review.intake_priority, "urgent_human_review")
        self.assertTrue(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.HUMAN_REVIEW_REQUIRED
        )

    def test_missing_referenced_police_report_page_is_retained(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                *complete_metadata().uploaded_evidence,
                UploadedEvidence(
                    evidence_type="police_report",
                    filename="police-report.pdf",
                    page_count=1,
                    expected_page_count=2,
                ),
            ]
        )
        model_review = ai_review(
            intake_complete=False,
            missing_documents=[
                MissingEvidence(
                    type="police_report_page_2",
                    reason="The report references page 2, but only page 1 is present.",
                    source_requirement="police_report",
                )
            ],
        )

        review = self.run_review(metadata=metadata, model_review=model_review)

        self.assertIn(
            "police_report_page_2",
            [item.type for item in review.missing_documents],
        )

    def test_invented_document_requirement_is_discarded(self) -> None:
        model_review = ai_review(
            intake_complete=False,
            missing_documents=[
                MissingEvidence(
                    type="unrelated_insurance_form",
                    reason="Invented requirement.",
                    source_requirement="invented_rule",
                )
            ],
        )

        review = self.run_review(model_review=model_review)

        self.assertNotIn(
            "unrelated_insurance_form",
            [item.type for item in review.missing_documents],
        )
        self.assertTrue(review.intake_complete)

    def test_valid_status_transition_is_accepted(self) -> None:
        current, target = validate_claim_status_transition(
            ClaimStatus.INTAKE_COMPLETE, ClaimStatus.REVIEW_PROCESSING
        )

        self.assertEqual(current, ClaimStatus.INTAKE_COMPLETE)
        self.assertEqual(target, ClaimStatus.REVIEW_PROCESSING)

    def test_invalid_status_transition_is_rejected(self) -> None:
        with self.assertRaises(InvalidClaimStatusTransition):
            validate_claim_status_transition(
                ClaimStatus.INTAKE_COMPLETE, ClaimStatus.INSPECTION_PENDING
            )

    def test_review_result_schema_rejects_missing_human_reason(self) -> None:
        with self.assertRaises(ValidationError):
            ReviewResult(
                intake_complete=False,
                intake_priority="urgent_human_review",
                priority_reason="Possible injury.",
                confidence=0.8,
                inspection_required=True,
                requires_human_review=True,
                human_review_reason=None,
            )


class InitialImageCapabilityRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock(name="gemini_client")
        self.service = ClaimReviewService(self.client, "configured-model-id")
        self.document = ClaimDocument(
            document_id="DOC-IMAGE",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="accident-photo.jpg",
            content_type="image/jpeg",
            storage_path="gs://demo/claims/CLM/documents/DOC/accident-photo.jpg",
            received_at=datetime.now(timezone.utc),
        )

    def _review(self, capabilities: ImageEvidenceCapabilities) -> tuple[ReviewResult, ClaimEvidenceMetadata]:
        intake = intake_result().model_copy(
            update={"image_evidence_capabilities": [capabilities]}
        )
        metadata = build_initial_review_metadata(intake, [self.document])
        self.client.models.generate_content.return_value.text = ai_review().model_dump_json()
        return self.service.review(intake, metadata), metadata

    def test_policy_hint_mismatch_creates_deterministic_known_conflict(self) -> None:
        intake = intake_result()

        metadata = build_initial_review_metadata(
            intake,
            [self.document],
            policy_number_hint="POL-DEMO-9999",
        )

        self.assertEqual(len(metadata.known_conflicts), 1)
        conflict = metadata.known_conflicts[0]
        self.assertEqual(conflict.field, "policy_number")
        self.assertEqual(conflict.values, ["POL-DEMO-9999", "POL-12345"])

    def test_readable_plate_in_original_photo_satisfies_both_identity_requirements(self) -> None:
        review, metadata = self._review(
            ImageEvidenceCapabilities(
                source="accident-photo.jpg",
                supported_capabilities=[
                    "damage_evidence",
                    "vehicle_identity",
                    "license_plate_photo",
                ],
                quality_observations=["The plate is visible and readable."],
            )
        )

        evidence_types = {
            item.evidence_type for item in metadata.uploaded_evidence
        }
        self.assertEqual(
            evidence_types,
            {"damage_evidence", "vehicle_identity", "license_plate_photo"},
        )
        self.assertTrue(metadata.vehicle_identity_clear)
        self.assertEqual(review.missing_documents, [])
        self.assertTrue(review.intake_complete)

    def test_original_photo_without_plate_requests_identity_evidence(self) -> None:
        review, metadata = self._review(
            ImageEvidenceCapabilities(
                source="accident-photo.jpg",
                supported_capabilities=["damage_evidence"],
                quality_observations=["No license plate is visible."],
            )
        )

        self.assertFalse(metadata.vehicle_identity_clear)
        self.assertEqual(
            {item.type for item in review.missing_documents},
            {"vehicle_identity", "license_plate_photo"},
        )

    def test_blurry_original_plate_is_not_credited(self) -> None:
        review, metadata = self._review(
            ImageEvidenceCapabilities(
                source="accident-photo.jpg",
                supported_capabilities=["damage_evidence"],
                unusable_capabilities=[
                    "vehicle_identity",
                    "license_plate_photo",
                ],
                quality_observations=["The plate is visible but too blurry to read."],
            )
        )

        self.assertFalse(metadata.vehicle_identity_clear)
        self.assertEqual(
            {item.type for item in review.missing_documents},
            {"vehicle_identity", "license_plate_photo"},
        )
        damage = next(
            item
            for item in metadata.uploaded_evidence
            if item.evidence_type == "damage_evidence"
        )
        self.assertIn("too blurry", damage.quality_observations[0])

    def test_same_readable_image_is_consistent_in_initial_and_resume_checks(self) -> None:
        capabilities = ImageEvidenceCapabilities(
            source="accident-photo.jpg",
            supported_capabilities=[
                "damage_evidence",
                "vehicle_identity",
                "license_plate_photo",
            ],
        )
        intake = intake_result().model_copy(
            update={"image_evidence_capabilities": [capabilities]}
        )
        metadata = build_initial_review_metadata(intake, [self.document])
        contradictory_review = ai_review(
            intake_complete=False,
            missing_documents=[
                MissingEvidence(
                    type="license_plate_photo",
                    reason="A plate photo is needed.",
                    source_requirement="vehicle_identity",
                )
            ],
            unusable_evidence=[
                UnusableEvidence(
                    evidence_type="license_plate_photo",
                    reason="The plate is unreadable.",
                    suggested_action="Upload another plate image.",
                )
            ],
        )
        self.client.models.generate_content.return_value.text = (
            contradictory_review.model_dump_json()
        )
        initial_review = self.service.review(intake, metadata)
        resume_client = MagicMock()
        resume_client.models.generate_content.return_value.text = (
            DocumentExtractionResult(
                usable=True,
                reason="The license plate is readable enough to verify identity.",
            ).model_dump_json()
        )
        resume_document = self.document.model_copy(
            update={"document_type": "license_plate_photo"}
        )

        resume_result = GeminiDocumentExtractor(
            resume_client, "configured-model-id"
        ).extract(resume_document, "vehicle_identity")

        self.assertTrue(metadata.vehicle_identity_clear)
        self.assertTrue(initial_review.intake_complete)
        self.assertEqual(initial_review.missing_documents, [])
        self.assertEqual(initial_review.unusable_evidence, [])
        self.assertTrue(resume_result.usable)
        self.assertEqual(resume_result.satisfies_requirement, "vehicle_identity")


if __name__ == "__main__":
    unittest.main()
