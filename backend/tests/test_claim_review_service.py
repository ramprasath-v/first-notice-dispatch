import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from google.genai import models as genai_models
from pydantic import ValidationError

from app.domain.claim_status import (
    ClaimStatus,
    InvalidClaimStatusTransition,
    review_target_status,
    validate_claim_status_transition,
)
from app.models.claim_document import ClaimDocument, DocumentExtractionResult
from app.models.intake_result import ImageEvidenceCapabilities, IntakeResult
from app.models.gemini_review_result import (
    GeminiRequestedAction,
    GeminiReviewResult,
)
from app.models.requested_action import (
    EnterTextRequestedAction,
    UploadDocumentRequestedAction,
)
from app.models.review_result import (
    ClaimEvidenceMetadata,
    ConflictSourceAssertion,
    CurrentEvidenceFinding,
    EvidenceConflict,
    MissingEvidence,
    OperationalIndicators,
    ReviewResult,
    SourceAwareConflict,
    UnresolvedUncertainty,
    UnusableEvidence,
    UploadedEvidence,
)
from app.services.claim_review_service import (
    ClaimReviewService,
    review_generation_config,
)
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


def review_assertions(
    field: str, assertions: list[tuple[str, str]]
) -> SourceAwareConflict:
    return SourceAwareConflict(
        fingerprint="provider-value-is-not-authoritative",
        field=field,
        assertions=[
            ConflictSourceAssertion(
                field=field,
                value=value,
                source_identity="provider-value-is-not-authoritative",
                filename=filename,
                document_id="provider-value-is-not-authoritative",
                replaceable=False,
            )
            for filename, value in assertions
        ],
        selected_outlier_document_id="provider-value-is-not-authoritative",
    )


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

    def test_complete_intake_routes_to_inspection_ready(self) -> None:
        review = self.run_review()

        self.assertTrue(review.intake_complete)
        self.assertEqual(review.intake_priority, "routine")
        self.assertEqual(
            review_target_status(review), ClaimStatus.INSPECTION_READY
        )
        call = self.client.models.generate_content.call_args
        self.assertEqual(call.kwargs["model"], "configured-model-id")
        self.assertEqual(call.kwargs["config"].temperature, 0.1)
        self.assertIs(call.kwargs["config"].response_schema, GeminiReviewResult)

    def test_compact_resumed_prompt_deduplicates_provider_evidence_context(self) -> None:
        intake = intake_result().model_copy(update={
            "evidence_findings": [
                CurrentEvidenceFinding(
                    source="plate.jpg", finding="Rear damage is visible."
                )
            ]
        })
        metadata = complete_metadata().model_copy(update={
            "uploaded_evidence": [
                UploadedEvidence(
                    evidence_type=capability,
                    filename="plate.jpg",
                    evidence_findings=["Rear damage is visible."],
                )
                for capability in (
                    "license_plate_photo", "vehicle_identity", "damage_evidence"
                )
            ]
        })
        snapshots = [{
            "document_id": "DOC-PLATE",
            "source": "plate.jpg",
            "document_type": "license_plate_photo",
            "status": "validated",
            "usable": True,
            "supported_capabilities": [
                "license_plate_photo", "vehicle_identity", "damage_evidence"
            ],
            "evidence_facts": {"license_plate": "7ABX123"},
            "evidence_findings": ["Rear damage is visible."],
        }]
        self.client.models.generate_content.return_value.text = (
            ai_review().model_dump_json()
        )

        self.service.review(
            intake,
            metadata,
            resumed_evidence_snapshots=snapshots,
        )

        prompt = self.client.models.generate_content.call_args.kwargs[
            "contents"
        ][0].parts[0].text
        self.assertIn("Current active evidence snapshots", prompt)
        self.assertEqual(prompt.count("Rear damage is visible."), 1)
        self.assertEqual(prompt.count("7ABX123"), 1)

    def test_initial_review_prompt_construction_is_unchanged(self) -> None:
        self.run_review()

        prompt = self.client.models.generate_content.call_args.kwargs[
            "contents"
        ][0].parts[0].text
        self.assertNotIn("Current active evidence snapshots", prompt)
        self.assertIn("Uploaded evidence metadata:", prompt)

    def test_production_review_schema_compiles_with_vertex_sdk(self) -> None:
        config = review_generation_config()
        api_client = MagicMock(vertexai=True)

        serialized = genai_models._GenerateContentConfig_to_vertex(
            api_client, config, {}
        )

        action_schema = serialized["responseSchema"].properties[
            "requested_actions"
        ].items
        schema_text = str(action_schema.model_dump(mode="json", exclude_none=True))
        self.assertNotIn("one_of", schema_text)
        self.assertNotIn("discriminator", schema_text)
        review_schema = serialized["responseSchema"]
        self.assertIn("review_outcome", review_schema.properties)
        self.assertIn("ambiguity_reason", review_schema.properties)
        self.assertIn("recommended_next_step", review_schema.properties)
        self.assertIn("ambiguity_summary", review_schema.properties)

    def test_review_instrumentation_survives_structured_output_and_rules(self) -> None:
        review = self.run_review(
            model_review=ai_review(
                review_outcome="ambiguous",
                ambiguity_reason="multiple_plausible_interpretations",
                recommended_next_step="retry_with_deeper_reasoning",
                ambiguity_summary="Two current sources support different damage sequences.",
            )
        )

        self.assertEqual(review.review_outcome, "ambiguous")
        self.assertEqual(
            review.ambiguity_reason, "multiple_plausible_interpretations"
        )
        self.assertEqual(review.recommended_next_step, "retry_with_deeper_reasoning")
        self.assertEqual(
            review.ambiguity_summary,
            "Two current sources support different damage sequences.",
        )

    def test_review_instrumentation_defaults_are_backward_compatible(self) -> None:
        review = ai_review()

        self.assertEqual(review.review_outcome, "resolved")
        self.assertIsNone(review.ambiguity_reason)
        self.assertEqual(review.recommended_next_step, "continue")
        self.assertIsNone(review.ambiguity_summary)

    def test_gemini_enter_text_action_converts_to_domain_action(self) -> None:
        gemini_action = GeminiRequestedAction(
            action_type="enter_text",
            action_id="ACT-TEXT",
            review_id="HRV-1",
            field_name="policy_number",
            document_type="police_report",
            replaces_document_id="DOC-IGNORED",
            instruction="Please confirm your policy number.",
        )
        action = gemini_action.to_domain()

        self.assertIsNone(gemini_action.document_type)
        self.assertIsNone(gemini_action.replaces_document_id)
        self.assertIsInstance(action, EnterTextRequestedAction)
        self.assertEqual(action.field_name, "policy_number")

    def test_gemini_upload_action_converts_to_domain_action(self) -> None:
        gemini_action = GeminiRequestedAction(
            action_type="upload_document",
            action_id="ACT-UPLOAD",
            review_id="HRV-1",
            document_type="damage_evidence",
            field_name="incident_summary",
            instruction="Please upload the correct damage photo.",
            replaces_document_id="DOC-ORIGINAL",
        )
        action = gemini_action.to_domain()

        self.assertIsNone(gemini_action.field_name)
        self.assertIsInstance(action, UploadDocumentRequestedAction)
        self.assertIsNone(action.replaces_document_id)

    def test_gemini_action_rejects_missing_relevant_fields(self) -> None:
        invalid_actions = [
            {
                "action_type": "enter_text",
                "action_id": "ACT-1",
                "review_id": "HRV-1",
                "instruction": "Confirm the value.",
            },
            {
                "action_type": "upload_document",
                "action_id": "ACT-2",
                "review_id": "HRV-1",
                "instruction": "Upload evidence.",
            },
        ]

        for candidate in invalid_actions:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValidationError):
                    GeminiRequestedAction.model_validate(candidate)

    def test_gemini_action_rejects_unknown_type_and_unknown_fields(self) -> None:
        invalid_actions = [
            {
                "action_type": "delete_document",
                "action_id": "ACT-1",
                "review_id": "HRV-1",
                "instruction": "Delete evidence.",
            },
            {
                "action_type": "enter_text",
                "action_id": "ACT-2",
                "review_id": "HRV-1",
                "field_name": "policy_number",
                "instruction": "Confirm the value.",
                "backend_operation": "replace_any_document",
            },
        ]

        for candidate in invalid_actions:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValidationError):
                    GeminiRequestedAction.model_validate(candidate)

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

    def test_single_unusable_replaceable_artifact_requests_replacement(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="unclear.jpg",
                    document_id="DOC-UNCLEAR",
                    source_identity="document:DOC-UNCLEAR",
                    document_type="damage_evidence",
                    status="unusable",
                    usable=False,
                    quality_observations=["The image is too blurry to assess."],
                ),
                UploadedEvidence(
                    evidence_type="vehicle_identity",
                    filename="identity.jpg",
                    document_id="DOC-IDENTITY",
                    source_identity="document:DOC-IDENTITY",
                    document_type="license_plate_photo",
                    status="validated",
                    usable=True,
                ),
            ],
            vehicle_identity_clear=True,
        )

        review = self.run_review(metadata=metadata)

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS)
        self.assertEqual(len(review.requested_actions), 1)
        self.assertEqual(
            review.requested_actions[0].replaces_document_id, "DOC-UNCLEAR"
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

    def test_low_confidence_with_resolvable_conflict_requests_claimant(
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

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review.intake_priority, "routine")
        self.assertEqual(review.requested_actions[0].action_type, "enter_text")
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
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
        self.assertTrue(review.operational_indicators.possible_injury)
        self.assertEqual(
            review_target_status(review), ClaimStatus.HUMAN_REVIEW_REQUIRED
        )

    def test_positive_provider_injury_survives_summary_omission(self) -> None:
        review = self.run_review(
            model_review=ai_review(
                operational_indicators=OperationalIndicators(
                    possible_injury=True
                )
            )
        )

        self.assertTrue(review.operational_indicators.possible_injury)
        self.assertTrue(review.requires_human_review)
        self.assertEqual(review.intake_priority, "urgent_human_review")
        self.assertEqual(
            review_target_status(review), ClaimStatus.HUMAN_REVIEW_REQUIRED
        )

    def test_no_injury_signal_keeps_existing_routine_route(self) -> None:
        review = self.run_review()

        self.assertFalse(review.operational_indicators.possible_injury)
        self.assertFalse(review.requires_human_review)
        self.assertEqual(review.intake_priority, "routine")
        self.assertEqual(review_target_status(review), ClaimStatus.INSPECTION_READY)

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

    def test_live_flow_4_safety_outlier_routes_to_claimant_remediation(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type="police_report", filename="police-report.pdf",
                document_id="DOC-REPORT", source_identity="document:DOC-REPORT",
                document_type="police_report", usable=True,
                evidence_findings=[
                    "Rear-end collision with rear bumper and left tail light damage."
                ],
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="IMG_5418.png",
                document_id="DOC-ORIGINAL", source_identity="document:DOC-ORIGINAL",
                document_type="damage_evidence", usable=True,
                evidence_findings=["Rear bumper and left tail light damage."],
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="IMG_5420.png",
                document_id="DOC-BAD", source_identity="document:DOC-BAD",
                document_type="license_plate_photo", usable=True,
                evidence_findings=[
                    "A silver SUV has front-end damage.",
                    "A material vehicle safety concern is visible.",
                ],
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="IMG_5419.png",
                document_id="DOC-CORRECT", source_identity="document:DOC-CORRECT",
                document_type="license_plate_photo", usable=True,
                evidence_findings=[
                    "A grey sedan has rear damage and California plate 7ABX123."
                ],
            ),
            UploadedEvidence(
                evidence_type="vehicle_identity", filename="IMG_5419.png",
                document_id="DOC-CORRECT", source_identity="document:DOC-CORRECT",
                document_type="license_plate_photo", usable=True,
            ),
            UploadedEvidence(
                evidence_type="license_plate_photo", filename="IMG_5419.png",
                document_id="DOC-CORRECT", source_identity="document:DOC-CORRECT",
                document_type="license_plate_photo", usable=True,
            ),
        ]
        model_review = ai_review(
            conflicts=[EvidenceConflict(
                field="vehicle_identity_and_damage_location",
                values=["silver SUV/front", "grey sedan/rear"],
                sources=["IMG_5420.png", "IMG_5419.png"],
                reason="The vehicle and damage evidence disagree.",
            )],
            current_evidence_findings=[
                CurrentEvidenceFinding(
                    source=item.filename, finding=finding
                )
                for item in uploaded
                for finding in item.evidence_findings
            ],
            unresolved_uncertainties=[UnresolvedUncertainty(
                uncertainty=(
                    "The silver front-damage vehicle and grey rear-damage vehicle "
                    "may not be the same vehicle."
                ),
                sources=["IMG_5420.png", "IMG_5419.png"],
            )],
            operational_indicators=OperationalIndicators(
                safety_concern=True,
                significant_damage=True,
                high_operational_uncertainty=True,
            ),
        )

        review = self.run_review(
            metadata=complete_metadata(
                uploaded_evidence=list(reversed(uploaded)),
                vehicle_identity_clear=True,
                safety_concern=True,
            ),
            model_review=model_review,
        )

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS)
        self.assertEqual(len(review.requested_actions), 1)
        self.assertEqual(
            review.requested_actions[0].replaces_document_id, "DOC-BAD"
        )
        self.assertEqual(
            review.source_aware_conflicts[0].selected_outlier_document_id,
            "DOC-BAD",
        )

    def test_production_flow_3_identity_request_targets_safe_damage_outlier(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="policy_document",
                    filename="policy.pdf",
                    document_id="DOC-POLICY",
                    source_identity="document:DOC-POLICY",
                    document_type="policy_document",
                    usable=True,
                    evidence_findings=["The insured vehicle is a Toyota Corolla."],
                ),
                UploadedEvidence(
                    evidence_type="police_report",
                    filename="police-report.pdf",
                    document_id="DOC-REPORT",
                    source_identity="document:DOC-REPORT",
                    document_type="police_report",
                    usable=True,
                    evidence_findings=["The involved vehicle is a Toyota Corolla."],
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="vehicle_damage_front.jpg",
                    document_id="DOC-FRONT",
                    source_identity="document:DOC-FRONT",
                    document_type="damage_evidence",
                    usable=True,
                    evidence_findings=["The photographed vehicle is a Honda SUV."],
                ),
            ],
            vehicle_identity_clear=False,
        )
        model_review = ai_review(
            conflicts=[
                EvidenceConflict(
                    field="vehicle_make",
                    values=["Toyota", "Toyota", "Honda"],
                    sources=[
                        "policy.pdf",
                        "police-report.pdf",
                        "vehicle_damage_front.jpg",
                    ],
                    reason="The policy and report identify a different vehicle.",
                ),
                EvidenceConflict(
                    field="vehicle_drivability",
                    values=["drivable", "potentially not drivable"],
                    sources=["police-report.pdf", "vehicle_damage_front.jpg"],
                    reason="The report and questionable photo disagree.",
                ),
                EvidenceConflict(
                    field="damage_location",
                    values=["rear", "front"],
                    sources=["police-report.pdf", "vehicle_damage_front.jpg"],
                    reason="The photo damage differs from the report.",
                ),
            ],
            current_evidence_findings=[
                CurrentEvidenceFinding(
                    source="police-report.pdf",
                    finding="Rear damage is reported and the vehicle is drivable.",
                ),
                CurrentEvidenceFinding(
                    source="vehicle_damage_front.jpg",
                    finding="Front damage and steam from the radiator are visible.",
                ),
            ],
            unresolved_uncertainties=[
                UnresolvedUncertainty(
                    uncertainty="Vehicle identity cannot be verified.",
                    sources=["vehicle_damage_front.jpg"],
                )
            ],
            operational_indicators=OperationalIndicators(
                safety_concern=True,
                significant_damage=True,
                high_operational_uncertainty=True,
            ),
        )

        review = self.run_review(metadata=metadata, model_review=model_review)

        self.assertEqual(
            {item.type for item in review.missing_documents},
            {"vehicle_identity", "license_plate_photo"},
        )
        self.assertFalse(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )
        self.assertEqual(len(review.requested_actions), 1)
        action = review.requested_actions[0]
        self.assertIsInstance(action, UploadDocumentRequestedAction)
        self.assertEqual(action.replaces_document_id, "DOC-FRONT")
        self.assertIn("readable license plate", action.instruction)

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

        review = self.run_review(
            intake=uncertain_intake,
            model_review=ai_review(
                operational_indicators=OperationalIndicators(
                    high_operational_uncertainty=True
                ),
                unresolved_uncertainties=[
                    UnresolvedUncertainty(
                        uncertainty="The safe inspection route remains ambiguous.",
                        sources=["accident-photo.jpg"],
                    )
                ],
            ),
        )

        self.assertEqual(review.missing_documents, [])
        self.assertEqual(review.unusable_evidence, [])
        self.assertEqual(review.intake_priority, "urgent_human_review")
        self.assertTrue(review.requires_human_review)
        self.assertEqual(
            review.human_review_reason,
            "FirstNotice could not formulate a safe claimant remediation.",
        )

    def test_historical_uncertainty_count_alone_does_not_trigger_human_review(
        self,
    ) -> None:
        historical = intake_result().model_copy(
            update={
                "uncertainties": [
                    "Vehicle identity was unavailable.",
                    "The license plate was not visible.",
                    "The vehicle identifier could not be confirmed.",
                ]
            }
        )

        review = self.run_review(intake=historical)

        self.assertEqual(review.unresolved_uncertainties, [])
        self.assertEqual(review.intake_priority, "routine")
        self.assertFalse(review.requires_human_review)

    def test_current_cross_evidence_conflict_is_source_attributed(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="police_report",
                    filename="police-report.pdf",
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="initial-damage.jpg",
                ),
                UploadedEvidence(
                    evidence_type="license_plate_photo",
                    filename="followup-identity.jpg",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="vehicle_identity",
                    filename="followup-identity.jpg",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="followup-identity.jpg",
                    usable=True,
                    evidence_findings=["Readable plate and rear damage are visible."],
                ),
            ],
            vehicle_identity_clear=True,
        )
        conflict = EvidenceConflict(
            field="damage_location",
            values=["rear damage", "severe front damage"],
            sources=[
                "police-report.pdf",
                "initial-damage.jpg",
                "followup-identity.jpg",
            ],
            reason=(
                "The initial photo shows front damage while the report and "
                "identified follow-up photo indicate rear damage."
            ),
        )
        model_review = ai_review(
            conflicts=[conflict],
            current_evidence_findings=[
                CurrentEvidenceFinding(
                    source="police-report.pdf",
                    finding="The report describes a rear-end collision and a drivable vehicle.",
                ),
                CurrentEvidenceFinding(
                    source="initial-damage.jpg",
                    finding="Severe front damage and a tow-truck condition are visible.",
                ),
                CurrentEvidenceFinding(
                    source="followup-identity.jpg",
                    finding="The plate is readable and rear damage is visible.",
                ),
            ],
            unresolved_uncertainties=[
                UnresolvedUncertainty(
                    uncertainty="Whether the initial front-damage photo belongs to this claim.",
                    sources=[
                        "initial-damage.jpg",
                        "police-report.pdf",
                        "followup-identity.jpg",
                    ],
                )
            ],
            operational_indicators=OperationalIndicators(
                high_operational_uncertainty=True
            ),
        )

        review = self.run_review(metadata=metadata, model_review=model_review)

        self.assertEqual(review.conflicts, [conflict])
        self.assertEqual(
            {finding.source for finding in review.current_evidence_findings},
            {
                "police-report.pdf",
                "initial-damage.jpg",
                "followup-identity.jpg",
            },
        )
        self.assertTrue(review.requires_human_review)
        self.assertEqual(review.requested_actions, [])
        self.assertEqual(review.intake_priority, "urgent_human_review")

    def test_approved_fingerprint_suppresses_only_unchanged_current_issue(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type="police_report", filename="report.pdf",
                document_id="DOC-REPORT", source_identity="document:DOC-REPORT",
                document_type="police_report", usable=True,
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="rear.jpg",
                document_id="DOC-REAR", source_identity="document:DOC-REAR",
                document_type="damage_evidence", usable=True,
            ),
            UploadedEvidence(
                evidence_type="license_plate_photo", filename="front.jpg",
                document_id="DOC-FRONT", source_identity="document:DOC-FRONT",
                document_type="license_plate_photo", usable=True,
            ),
            UploadedEvidence(
                evidence_type="vehicle_identity", filename="front.jpg",
                document_id="DOC-FRONT", source_identity="document:DOC-FRONT",
                document_type="license_plate_photo", usable=True,
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="front.jpg",
                document_id="DOC-FRONT", source_identity="document:DOC-FRONT",
                document_type="license_plate_photo", usable=True,
            ),
        ]
        metadata = complete_metadata(
            uploaded_evidence=uploaded, vehicle_identity_clear=True
        )
        conflict = EvidenceConflict(
            field="damage_location", values=["rear", "front"],
            sources=["report.pdf", "rear.jpg", "front.jpg"],
            reason="The front image conflicts with the corroborated rear damage.",
        )
        findings = [
            CurrentEvidenceFinding(source="report.pdf", finding="Rear-end damage."),
            CurrentEvidenceFinding(source="rear.jpg", finding="Rear damage visible."),
            CurrentEvidenceFinding(source="front.jpg", finding="Front-end damage visible."),
        ]
        model = ai_review(
            conflicts=[conflict], current_evidence_findings=findings,
            unresolved_uncertainties=[UnresolvedUncertainty(
                uncertainty="The front versus rear damage location remains unclear.",
                sources=["report.pdf", "rear.jpg", "front.jpg"],
            )],
            operational_indicators=OperationalIndicators(
                high_operational_uncertainty=True
            ),
        )

        first = self.run_review(metadata=metadata, model_review=model)
        fingerprint = first.source_aware_conflicts[0].fingerprint
        approved = metadata.model_copy(
            update={"approved_issue_fingerprints": [fingerprint]}
        )
        second = self.run_review(metadata=approved, model_review=model)

        self.assertEqual(second.conflicts, [])
        self.assertEqual(second.unresolved_uncertainties, [])
        self.assertFalse(second.requires_human_review)

    def test_flow_4_uncertainty_selects_corroborated_front_image(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type="police_report", filename="police-report.pdf",
                document_id="DOC-REPORT", source_identity="document:DOC-REPORT",
                document_type="police_report", usable=True,
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="vehicle_damage.jpg",
                document_id="DOC-REAR", source_identity="document:DOC-REAR",
                document_type="damage_evidence", usable=True,
            ),
            UploadedEvidence(
                evidence_type="license_plate_photo",
                filename="vehicle_damage_front.jpg", document_id="DOC-FRONT",
                source_identity="document:DOC-FRONT",
                document_type="license_plate_photo", usable=True,
            ),
            UploadedEvidence(
                evidence_type="vehicle_identity",
                filename="vehicle_damage_front.jpg", document_id="DOC-FRONT",
                source_identity="document:DOC-FRONT",
                document_type="license_plate_photo", usable=True,
            ),
            UploadedEvidence(
                evidence_type="damage_evidence",
                filename="vehicle_damage_front.jpg", document_id="DOC-FRONT",
                source_identity="document:DOC-FRONT",
                document_type="license_plate_photo", usable=True,
            ),
        ]
        findings = [
            CurrentEvidenceFinding(
                source="police-report.pdf", finding="Rear-end collision damage."
            ),
            CurrentEvidenceFinding(
                source="vehicle_damage.jpg", finding="Rear damage is visible."
            ),
            CurrentEvidenceFinding(
                source="vehicle_damage_front.jpg", finding="Front-end damage is visible."
            ),
        ]
        model = ai_review(
            conflicts=[], current_evidence_findings=findings,
            unresolved_uncertainties=[UnresolvedUncertainty(
                uncertainty=(
                    "The relationship of the front-end vehicle in "
                    "vehicle_damage_front.jpg to the rear-end collision vehicle "
                    "in vehicle_damage.jpg is unclear."
                ),
                sources=["vehicle_damage.jpg", "vehicle_damage_front.jpg"],
                source_attribution_incomplete=False,
            )],
            operational_indicators=OperationalIndicators(
                high_operational_uncertainty=True
            ),
        )

        review = self.run_review(
            metadata=complete_metadata(
                uploaded_evidence=uploaded, vehicle_identity_clear=True
            ),
            model_review=model,
        )

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review.requested_actions[0].action_type, "upload_document")
        self.assertEqual(review.conflicts, [])
        self.assertEqual(
            review.source_aware_uncertainties[0].selected_outlier_document_id,
            "DOC-FRONT",
        )

    def test_one_vs_one_report_and_wrong_photo_requires_human_review(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type="police_report", filename="police-report.pdf",
                document_id="DOC-REPORT", source_identity="document:DOC-REPORT",
                document_type="police_report", usable=True,
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="front.jpg",
                document_id="DOC-FRONT", source_identity="document:DOC-FRONT",
                document_type="damage_evidence", usable=True,
            ),
        ]
        conflict = EvidenceConflict(
            field="damage_location", values=["rear", "front"],
            sources=["police-report.pdf", "front.jpg"],
            reason="The photo damage location differs from the report.",
        )

        model_review = ai_review(
            conflicts=[conflict],
            current_evidence_findings=[
                CurrentEvidenceFinding(
                    source="police-report.pdf", finding="Rear-end damage."
                ),
                CurrentEvidenceFinding(
                    source="front.jpg", finding="Front-end damage."
                ),
            ],
        )
        response_payload = model_review.model_dump(mode="json")
        response_payload["requested_actions"] = [
            {
                "action_type": "upload_document",
                "action_id": "ACT-GEMINI",
                "review_id": "HRV-GEMINI",
                "field_name": "incident_summary",
                "document_type": "damage_evidence",
                "instruction": "Please upload the correct damage photo.",
                "replaces_document_id": "DOC-GEMINI-CANNOT-AUTHORIZE",
            }
        ]
        self.client.models.generate_content.return_value.text = json.dumps(
            response_payload
        )

        review = self.service.review(
            intake_result(),
            complete_metadata(
                uploaded_evidence=uploaded, vehicle_identity_clear=True
            ),
        )

        self.assertTrue(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.HUMAN_REVIEW_REQUIRED
        )
        self.assertEqual(review.requested_actions, [])

    def test_changed_evidence_set_produces_new_review_fingerprint(self) -> None:
        base = [
            UploadedEvidence(
                evidence_type="police_report", filename="report.pdf",
                document_id="DOC-REPORT", source_identity="document:DOC-REPORT",
                document_type="police_report", usable=True,
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="rear.jpg",
                document_id="DOC-REAR", source_identity="document:DOC-REAR",
                document_type="damage_evidence", usable=True,
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="front.jpg",
                document_id="DOC-FRONT", source_identity="document:DOC-FRONT",
                document_type="license_plate_photo", usable=True,
            ),
            UploadedEvidence(
                evidence_type="vehicle_identity", filename="front.jpg",
                document_id="DOC-FRONT", source_identity="document:DOC-FRONT",
                document_type="license_plate_photo", usable=True,
            ),
        ]
        first_conflict = EvidenceConflict(
            field="damage_location", values=["rear", "front"],
            sources=["report.pdf", "rear.jpg", "front.jpg"], reason="Conflict.",
        )
        first_findings = [
            CurrentEvidenceFinding(source="report.pdf", finding="Rear damage."),
            CurrentEvidenceFinding(source="rear.jpg", finding="Rear damage."),
            CurrentEvidenceFinding(source="front.jpg", finding="Front damage."),
        ]
        first = self.run_review(
            metadata=complete_metadata(uploaded_evidence=base, vehicle_identity_clear=True),
            model_review=ai_review(
                conflicts=[first_conflict], current_evidence_findings=first_findings
            ),
        )
        approved_fingerprint = first.source_aware_conflicts[0].fingerprint
        added = UploadedEvidence(
            evidence_type="damage_evidence", filename="new-rear.jpg",
            document_id="DOC-NEW", source_identity="document:DOC-NEW",
            document_type="damage_evidence", usable=True,
        )
        changed_conflict = first_conflict.model_copy(update={
            "sources": ["report.pdf", "rear.jpg", "new-rear.jpg", "front.jpg"]
        })
        changed_findings = [
            *first_findings,
            CurrentEvidenceFinding(source="new-rear.jpg", finding="Rear damage."),
        ]
        changed_metadata = complete_metadata(
            uploaded_evidence=[*base, added], vehicle_identity_clear=True,
            approved_issue_fingerprints=[approved_fingerprint],
        )

        changed = self.run_review(
            metadata=changed_metadata,
            model_review=ai_review(
                conflicts=[changed_conflict], current_evidence_findings=changed_findings
            ),
        )

        self.assertNotEqual(
            changed.source_aware_conflicts[0].fingerprint, approved_fingerprint
        )
        self.assertFalse(changed.requires_human_review)
        self.assertEqual(len(changed.requested_actions), 1)

    def test_two_image_uncertainty_preserves_both_grounded_sources(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="silver-suv.jpg",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="grey-sedan.jpg",
                    usable=True,
                ),
            ],
            vehicle_identity_clear=True,
        )
        uncertainty = UnresolvedUncertainty(
            uncertainty="The two submitted photos show different vehicles.",
            sources=["silver-suv.jpg", "grey-sedan.jpg"],
        )

        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                unresolved_uncertainties=[uncertainty],
                operational_indicators=OperationalIndicators(
                    high_operational_uncertainty=True
                ),
            ),
        )

        persisted = review.unresolved_uncertainties[0]
        self.assertEqual(
            persisted.sources, ["silver-suv.jpg", "grey-sedan.jpg"]
        )
        self.assertFalse(persisted.source_attribution_incomplete)

    def test_cross_image_uncertainty_marks_incomplete_source_attribution(self) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="silver-suv.jpg",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="grey-sedan.jpg",
                    usable=True,
                ),
            ],
            vehicle_identity_clear=True,
        )

        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                unresolved_uncertainties=[UnresolvedUncertainty(
                    uncertainty="The two submitted photos show different vehicles.",
                    sources=["silver-suv.jpg", "unmatched-second-photo.jpg"],
                )],
                operational_indicators=OperationalIndicators(
                    high_operational_uncertainty=True
                ),
            ),
        )

        persisted = review.unresolved_uncertainties[0]
        self.assertEqual(persisted.sources, ["silver-suv.jpg"])
        self.assertTrue(persisted.source_attribution_incomplete)
        self.assertTrue(review.requires_human_review)

    def test_damage_conflict_waits_for_resolvable_identity_evidence_first(
        self,
    ) -> None:
        metadata = complete_metadata(
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="police_report",
                    filename="police-report.pdf",
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="initial-damage.jpg",
                    usable=True,
                ),
            ],
            vehicle_identity_clear=False,
        )
        conflict = EvidenceConflict(
            field="damage_location",
            values=["rear", "front"],
            sources=["police-report.pdf", "initial-damage.jpg"],
            reason="The submitted sources show different damage locations.",
        )

        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                conflicts=[conflict],
                operational_indicators=OperationalIndicators(
                    high_operational_uncertainty=True
                ),
                unresolved_uncertainties=[
                    UnresolvedUncertainty(
                        uncertainty="The initial photo vehicle identity is unclear.",
                        sources=["initial-damage.jpg", "police-report.pdf"],
                    )
                ],
            ),
        )

        self.assertEqual(
            {item.type for item in review.missing_documents},
            {"vehicle_identity", "license_plate_photo"},
        )
        self.assertFalse(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
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

    def test_unresolved_minor_conflict_blocks_operational_completeness(self) -> None:
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

        self.assertFalse(review.intake_complete)
        self.assertTrue(review.requires_human_review)

    def test_structured_conflict_requests_claimant_correction(self) -> None:
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

        self.assertEqual(review.intake_priority, "routine")
        self.assertFalse(review.requires_human_review)
        self.assertEqual(review.requested_actions[0].action_type, "enter_text")
        self.assertEqual(
            review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS
        )

    def test_missing_policy_and_corroborated_mismatch_preserve_replacement_target(
        self,
    ) -> None:
        metadata = complete_metadata(
            vehicle_identity_clear=True,
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="policy_document",
                    filename="policy.pdf",
                    document_id="DOC-POLICY",
                    source_identity="document:DOC-POLICY",
                    document_type="policy_document",
                    usable=True,
                    evidence_findings=["The insured vehicle is a Honda Civic."],
                ),
                UploadedEvidence(
                    evidence_type="police_report",
                    filename="report.pdf",
                    document_id="DOC-REPORT",
                    source_identity="document:DOC-REPORT",
                    document_type="police_report",
                    usable=True,
                    evidence_findings=["The involved vehicle is a Toyota Camry."],
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="vehicle.jpg",
                    document_id="DOC-PHOTO",
                    source_identity="document:DOC-PHOTO",
                    document_type="damage_evidence",
                    usable=True,
                    evidence_findings=["The photographed vehicle is a Toyota Camry."],
                ),
                UploadedEvidence(
                    evidence_type="vehicle_identity",
                    filename="vehicle.jpg",
                    document_id="DOC-PHOTO",
                    source_identity="document:DOC-PHOTO",
                    document_type="damage_evidence",
                    usable=True,
                ),
            ],
        )
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["Honda Civic", "Toyota Camry", "Toyota Camry"],
            sources=["policy.pdf", "report.pdf", "vehicle.jpg"],
            reason="The policy vehicle differs from corroborating current evidence.",
        )

        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                intake_complete=False,
                missing_documents=[
                    MissingEvidence(
                        type="policy_document",
                        reason="The current policy evidence conflicts with the claim.",
                        source_requirement="policy_document",
                    )
                ],
                conflicts=[conflict],
                review_outcome="claimant_remediable",
                recommended_next_step="request_claimant_evidence",
            ),
        )

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review.review_outcome, "claimant_remediable")
        self.assertEqual(review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS)
        self.assertEqual(len(review.requested_actions), 1)
        action = review.requested_actions[0]
        self.assertEqual(action.document_type, "policy_document")
        self.assertEqual(action.replaces_document_id, "DOC-POLICY")

    def test_persisted_facts_override_provider_human_review_recommendation(
        self,
    ) -> None:
        metadata = complete_metadata(
            vehicle_identity_clear=True,
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="policy_document",
                    filename="policy.pdf",
                    document_id="DOC-POLICY",
                    source_identity="document:DOC-POLICY",
                    document_type="policy_document",
                    status="received",
                    evidence_findings=[
                        "vehicle_identity: 2022 Honda Accord",
                        "vehicle_make: Honda",
                        "vehicle_model: Accord",
                        "vehicle_year: 2022",
                    ],
                ),
                UploadedEvidence(
                    evidence_type="police_report",
                    filename="report.pdf",
                    document_id="DOC-REPORT",
                    source_identity="document:DOC-REPORT",
                    document_type="police_report",
                    status="received",
                    evidence_findings=[
                        "vehicle_identity: 2014 Toyota Corolla",
                        "vehicle_make: Toyota",
                        "vehicle_model: Corolla",
                        "vehicle_year: 2014",
                    ],
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="vehicle.png",
                    document_id="DOC-PHOTO",
                    source_identity="document:DOC-PHOTO",
                    document_type="damage_evidence",
                    status="received",
                    evidence_findings=[
                        "vehicle_identity: Toyota Corolla",
                        "vehicle_make: Toyota",
                        "vehicle_model: Corolla",
                    ],
                ),
            ],
        )
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["2022 Honda Accord", "2014 Toyota Corolla"],
            sources=["policy.pdf", "report.pdf", "vehicle.png"],
            reason="The policy vehicle differs from the incident evidence.",
        )

        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                intake_complete=False,
                conflicts=[conflict],
                source_aware_conflicts=[],
                review_outcome="requires_human_judgment",
                ambiguity_reason="policy_or_business_judgment",
                recommended_next_step="human_review",
            ),
        )

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS)
        self.assertEqual(len(review.requested_actions), 1)
        action = review.requested_actions[0]
        self.assertEqual(action.document_type, "policy_document")
        self.assertEqual(action.replaces_document_id, "DOC-POLICY")

    def test_received_unusable_policy_and_safe_outlier_preserve_replacement_target(
        self,
    ) -> None:
        metadata = complete_metadata(
            vehicle_identity_clear=True,
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="policy_document",
                    filename="policy.pdf",
                    document_id="DOC-POLICY",
                    source_identity="document:DOC-POLICY",
                    document_type="policy_document",
                    status="received",
                    evidence_findings=["The insured vehicle is a Honda Civic."],
                ),
                UploadedEvidence(
                    evidence_type="police_report",
                    filename="report.pdf",
                    document_id="DOC-REPORT",
                    source_identity="document:DOC-REPORT",
                    document_type="police_report",
                    usable=True,
                    evidence_findings=["The involved vehicle is a Toyota Camry."],
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="vehicle.jpg",
                    document_id="DOC-PHOTO",
                    source_identity="document:DOC-PHOTO",
                    document_type="damage_evidence",
                    usable=True,
                    evidence_findings=["The photographed vehicle is a Toyota Camry."],
                ),
            ],
        )
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["Honda Civic", "Toyota Camry", "Toyota Camry"],
            sources=["policy.pdf", "report.pdf", "vehicle.jpg"],
            reason="The policy vehicle differs from corroborating current evidence.",
        )

        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                intake_complete=False,
                unusable_evidence=[
                    UnusableEvidence(
                        evidence_type="policy_document",
                        reason="The policy vehicle conflicts with current evidence.",
                        suggested_action="Upload the correct policy document.",
                    )
                ],
                conflicts=[conflict],
                review_outcome="claimant_remediable",
                recommended_next_step="request_claimant_evidence",
            ),
        )

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS)
        self.assertEqual(len(review.requested_actions), 1)
        action = review.requested_actions[0]
        self.assertEqual(action.document_type, "policy_document")
        self.assertEqual(action.replaces_document_id, "DOC-POLICY")

    def test_source_aligned_two_vs_one_selects_replaceable_outlier(self) -> None:
        metadata = complete_metadata(
            vehicle_identity_clear=True,
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="police_report",
                    filename="source-a.pdf",
                    document_id="DOC-A",
                    source_identity="document:DOC-A",
                    document_type="police_report",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="source-b.jpg",
                    document_id="DOC-B",
                    source_identity="document:DOC-B",
                    document_type="damage_evidence",
                    usable=True,
                    evidence_findings=["The artifact states value one."],
                ),
                UploadedEvidence(
                    evidence_type="vehicle_identity",
                    filename="source-b.jpg",
                    document_id="DOC-B",
                    source_identity="document:DOC-B",
                    document_type="damage_evidence",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="source-c.jpg",
                    document_id="DOC-C",
                    source_identity="document:DOC-C",
                    document_type="damage_evidence",
                    usable=True,
                ),
            ],
        )
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["value one", "value two"],
            sources=["source-a.pdf", "source-c.jpg"],
            reason="The submitted sources disagree.",
        )
        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                intake_complete=False,
                conflicts=[conflict],
                source_aware_conflicts=[review_assertions(
                    "vehicle_identity",
                    [
                        ("source-a.pdf", "value one"),
                        ("source-c.jpg", "value two"),
                    ],
                )],
            ),
        )

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review.requested_actions[0].replaces_document_id, "DOC-C")
        self.assertEqual(
            review.source_aware_conflicts[0].selected_outlier_document_id,
            "DOC-C",
        )

    def test_all_active_reconstruction_one_vs_one_does_not_guess(self) -> None:
        metadata = complete_metadata(
            vehicle_identity_clear=True,
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type="police_report",
                    filename="source-a.pdf",
                    document_id="DOC-A",
                    source_identity="document:DOC-A",
                    document_type="police_report",
                    usable=True,
                ),
                UploadedEvidence(
                    evidence_type="damage_evidence",
                    filename="source-b.jpg",
                    document_id="DOC-B",
                    source_identity="document:DOC-B",
                    document_type="damage_evidence",
                    usable=True,
                ),
            ],
        )
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["value one", "value two"],
            sources=["source-a.pdf", "source-b.jpg"],
            reason="The two sources disagree.",
        )
        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                intake_complete=False,
                conflicts=[conflict],
                source_aware_conflicts=[review_assertions(
                    "vehicle_identity",
                    [
                        ("source-a.pdf", "value one"),
                        ("source-b.jpg", "value two"),
                    ],
                )],
            ),
        )

        self.assertIsNone(
            review.source_aware_conflicts[0].selected_outlier_document_id
        )
        self.assertEqual(review.requested_actions, [])
        self.assertTrue(review.requires_human_review)

    def test_partial_atomic_vehicle_facts_create_targeted_composite_replacement(
        self,
    ) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type=document_type,
                filename=filename,
                document_id=document_id,
                source_identity=f"document:{document_id}",
                document_type=document_type,
                status=status,
                usable=usable,
                evidence_findings=findings,
            )
            for document_type, filename, document_id, status, usable, findings in (
                (
                    "policy_document", "insurance2.pdf", "DOC-POLICY",
                    "received", None,
                    [
                        "vehicle_identity: 2014 Toyota Corolla (Dark Grey)",
                        "vehicle_make: Toyota", "vehicle_model: Corolla",
                        "vehicle_year: 2014", "license_plate: 7ABX123",
                    ],
                ),
                (
                    "police_report", "policeReport2.pdf", "DOC-REPORT",
                    "received", None,
                    [
                        "vehicle_identity: 2014 Toyota Corolla (Dark Grey)",
                        "vehicle_make: Toyota", "vehicle_model: Corolla",
                        "vehicle_year: 2014", "license_plate: 7ABX123",
                    ],
                ),
                (
                    "license_plate_photo", "image3.jpg", "DOC-WRONG",
                    "unusable", False,
                    ["vehicle_identity: Honda SUV", "vehicle_make: Honda"],
                ),
                (
                    "license_plate_photo", "imageL2.png", "DOC-CORRECT",
                    "validated", True,
                    [
                        "vehicle_identity: Toyota Corolla",
                        "vehicle_make: Toyota", "vehicle_model: Corolla",
                        "license_plate: 7ABX123",
                    ],
                ),
            )
        ]
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["Honda SUV", "2014 Toyota Corolla (Dark Grey)"],
            sources=["image3.jpg", "insurance2.pdf"],
            reason="The submitted sources identify different vehicles.",
        )

        review = self.run_review(
            metadata=complete_metadata(
                uploaded_evidence=uploaded,
                vehicle_identity_clear=True,
            ),
            model_review=ai_review(
                intake_complete=False,
                conflicts=[conflict],
                review_outcome="requires_human_judgment",
                ambiguity_reason="multiple_plausible_interpretations",
                recommended_next_step="human_review",
                ambiguity_summary="The submitted images appear inconsistent.",
            ),
        )

        self.assertFalse(review.requires_human_review)
        self.assertEqual(len(review.requested_actions), 1)
        action = review.requested_actions[0]
        self.assertIsInstance(action, UploadDocumentRequestedAction)
        self.assertEqual(action.replaces_document_id, "DOC-WRONG")
        self.assertEqual(
            review.source_aware_conflicts[0].selected_outlier_document_id,
            "DOC-WRONG",
        )

    def test_source_aligned_three_way_conflict_does_not_guess(self) -> None:
        metadata = complete_metadata(
            vehicle_identity_clear=True,
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type=document_type,
                    filename=filename,
                    document_id=document_id,
                    source_identity=f"document:{document_id}",
                    document_type=document_type,
                    usable=True,
                )
                for document_type, filename, document_id in (
                    ("police_report", "source-a.pdf", "DOC-A"),
                    ("damage_evidence", "source-b.jpg", "DOC-B"),
                    ("damage_evidence", "source-c.jpg", "DOC-C"),
                )
            ],
        )
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["unmapped", "values"],
            sources=["source-a.pdf", "source-b.jpg", "source-c.jpg"],
            reason="The sources disagree.",
        )
        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                intake_complete=False,
                conflicts=[conflict],
                source_aware_conflicts=[review_assertions(
                    "vehicle_identity",
                    [
                        ("source-a.pdf", "value one"),
                        ("source-b.jpg", "value two"),
                        ("source-c.jpg", "value three"),
                    ],
                )],
            ),
        )

        self.assertEqual(review.requested_actions, [])
        self.assertTrue(review.requires_human_review)

    def test_incomplete_source_assertions_do_not_select_outlier(self) -> None:
        metadata = complete_metadata(
            vehicle_identity_clear=True,
            uploaded_evidence=[
                UploadedEvidence(
                    evidence_type=document_type,
                    filename=filename,
                    document_id=document_id,
                    source_identity=f"document:{document_id}",
                    document_type=document_type,
                    usable=True,
                )
                for document_type, filename, document_id in (
                    ("police_report", "source-a.pdf", "DOC-A"),
                    ("damage_evidence", "source-b.jpg", "DOC-B"),
                    ("damage_evidence", "source-c.jpg", "DOC-C"),
                )
            ],
        )
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["unmapped", "values"],
            sources=["source-a.pdf", "source-b.jpg", "source-c.jpg"],
            reason="The sources disagree.",
        )
        review = self.run_review(
            metadata=metadata,
            model_review=ai_review(
                intake_complete=False,
                conflicts=[conflict],
                source_aware_conflicts=[review_assertions(
                    "vehicle_identity",
                    [
                        ("source-a.pdf", "value one"),
                        ("source-c.jpg", "value two"),
                    ],
                )],
            ),
        )

        self.assertIsNone(
            review.source_aware_conflicts[0].selected_outlier_document_id
        )
        self.assertEqual(review.requested_actions, [])
        self.assertTrue(review.requires_human_review)

    def test_three_way_vehicle_conflict_requires_human_review(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type=document_type,
                filename=filename,
                document_id=document_id,
                source_identity=f"document:{document_id}",
                document_type=document_type,
                usable=True,
            )
            for document_type, filename, document_id in (
                ("policy_document", "policy.pdf", "DOC-POLICY"),
                ("police_report", "report.pdf", "DOC-REPORT"),
                ("damage_evidence", "vehicle.jpg", "DOC-PHOTO"),
            )
        ]
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["Honda", "Toyota", "Nissan"],
            sources=["policy.pdf", "report.pdf", "vehicle.jpg"],
            reason="Each source identifies a different vehicle.",
        )

        review = self.run_review(
            metadata=complete_metadata(
                uploaded_evidence=uploaded,
                vehicle_identity_clear=True,
            ),
            model_review=ai_review(intake_complete=False, conflicts=[conflict]),
        )

        self.assertEqual(review.requested_actions, [])
        self.assertTrue(review.requires_human_review)
        self.assertEqual(
            review_target_status(review), ClaimStatus.HUMAN_REVIEW_REQUIRED
        )

    def test_missing_policy_number_requests_policy_document(self) -> None:
        review = self.run_review(
            intake=intake_result().model_copy(update={"policy_number": None})
        )

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS)
        self.assertEqual(review.requested_actions[0].document_type, "policy_document")

    def test_missing_required_police_report_requests_report(self) -> None:
        review = self.run_review(
            metadata=complete_metadata(police_attended=True)
        )

        self.assertFalse(review.requires_human_review)
        self.assertEqual(review_target_status(review), ClaimStatus.AWAITING_DOCUMENTS)
        self.assertEqual(review.requested_actions[0].document_type, "police_report")

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
                supported_capabilities=[
                    "license_plate_photo",
                    "vehicle_identity",
                    "damage_evidence",
                ],
                evidence_findings=["The plate is readable and rear damage is visible."],
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
        self.assertIn("damage_evidence", resume_result.supported_capabilities)
        self.assertIn("rear damage", resume_result.evidence_findings[0])


if __name__ == "__main__":
    unittest.main()
