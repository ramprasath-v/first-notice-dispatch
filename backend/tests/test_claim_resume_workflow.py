import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from google.genai import types

from app.domain.claim_status import ClaimStatus, review_target_status
from app.models.claim_document import ClaimDocument, DocumentExtractionResult
from app.models.intake_result import EvidenceArtifactFacts
from app.models.requested_action import (
    UploadDocumentRequestedAction,
    parse_requested_actions,
)
from app.models.review_result import (
    CurrentEvidenceFinding,
    EvidenceConflict,
    OperationalIndicators,
    ReviewResult,
)
from app.services.claim_review_service import ClaimReviewService
from app.services.claim_review_service import ClaimReviewError
from app.services.document_extraction_service import (
    UnsupportedResumeDocumentTypeError,
)
from app.tools.firestore_repository import FirestoreWriteError
from app.workflows.claim_resume_workflow import (
    ClaimResumeError,
    ClaimResumeWorkflow,
    _build_review_metadata,
    _current_review_intake_result,
    match_missing_requirement,
)


def awaiting_claim() -> dict[str, object]:
    return {
        "claim_id": "CLM-A1B2C3D4",
        "status": "awaiting_documents",
        "claim_type": "auto_collision",
        "damage_type": "Rear bumper damage",
        "parts_affected": ["rear bumper"],
        "incident_summary": "The vehicle was struck from behind.",
        "incident_description": "The vehicle was struck from behind.",
        "policy_number": "POL-123",
        "incident_date": "2026-08-05",
        "vehicle_drivable": True,
        "uncertainties": [],
        "missing_documents": [
            {
                "type": "license_plate_photo",
                "reason": "A clear plate photo is required.",
                "source_requirement": "license_plate_photo",
            }
        ],
        "unusable_evidence": [],
        "conflicts": [],
    }


def document(
    *, document_id: str = "DOC-12345678", document_type: str = "license_plate_photo"
) -> ClaimDocument:
    return ClaimDocument(
        document_id=document_id,
        claim_id="CLM-A1B2C3D4",
        document_type=document_type,
        filename="license-plate.jpg",
        storage_path="/demo/license-plate.jpg",
        received_at=datetime.now(timezone.utc),
    )


def review_result(
    *,
    complete: bool = True,
    human_review: bool = False,
) -> ReviewResult:
    return ReviewResult(
        intake_complete=complete,
        intake_priority=("urgent_human_review" if human_review else "routine"),
        priority_reason=(
            "A significant factual conflict requires human review."
            if human_review
            else "No urgent operational routing indicator was identified."
        ),
        confidence=0.9,
        inspection_required=True,
        requires_human_review=human_review,
        human_review_reason=(
            "A significant factual conflict requires human review."
            if human_review
            else None
        ),
    )


class ClaimResumeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.claim = awaiting_claim()
        self.documents: dict[str, ClaimDocument] = {}
        self.repository = MagicMock(name="claim_repository")
        self.review_service = MagicMock(name="review_service")
        self.extractor = MagicMock(name="document_extractor")

        self.repository.get_claim.side_effect = lambda claim_id: dict(self.claim)
        self.repository.get_document.side_effect = (
            lambda claim_id, document_id: self.documents.get(document_id)
        )
        self.repository.get_documents.side_effect = (
            lambda claim_id: list(self.documents.values())
        )
        self.repository.add_document.side_effect = self._add_document
        self.repository.mark_document_validated.side_effect = self._mark_validated
        self.repository.mark_document_unusable.side_effect = self._mark_unusable
        self.repository.begin_document_resume_review.side_effect = (
            self._begin_document_resume_review
        )
        self.repository.save_document_resume_extraction.side_effect = (
            self._save_document_resume_extraction
        )
        self.repository.mark_document_resume_quality_processed.side_effect = (
            self._mark_document_resume_quality_processed
        )
        self.repository.mark_document_resume_retry_required.side_effect = (
            self._mark_document_resume_retry_required
        )
        self.repository.mark_document_resume_rejected.side_effect = (
            self._mark_document_resume_rejected
        )
        self.repository.complete_requested_evidence_item.side_effect = (
            self._complete_requested_evidence_item
        )
        self.repository.update_claim_status.side_effect = self._update_status
        self.repository.save_review_result.side_effect = self._save_review
        self.workflow = ClaimResumeWorkflow(
            repository=self.repository,
            review_service=self.review_service,
            document_extractor=self.extractor,
        )

    def _add_document(self, value: ClaimDocument) -> None:
        self.documents[value.document_id] = value

    def _mark_validated(self, claim_id: str, document_id: str, **fields) -> None:
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={"status": "validated", **fields}
        )

    def _mark_unusable(
        self, claim_id: str, document_id: str, reason: str, **fields
    ) -> None:
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={"status": "unusable", "quality_reason": reason, **fields}
        )

    def _update_status(self, claim_id: str, status: ClaimStatus) -> ClaimStatus:
        self.claim["status"] = ClaimStatus(status).value
        return ClaimStatus(status)

    def _begin_document_resume_review(
        self,
        claim_id: str,
        document_id: str,
        *,
        idempotency_key: str,
        matched_requirement: str,
        correlation_id: str,
        replacement_action_id: str | None = None,
        replaces_document_id: str | None = None,
        replacement_document_type: str | None = None,
    ) -> bool:
        self.claim.update(
            {
                "status": "review_processing",
                "active_resume_document_id": document_id,
                "active_resume_idempotency_key": idempotency_key,
                "active_resume_correlation_id": correlation_id,
            }
        )
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={
                "resume_idempotency_key": idempotency_key,
                "resume_correlation_id": correlation_id,
                "resume_matched_requirement": matched_requirement,
                "resume_started_at": datetime.now(timezone.utc),
                "requested_action_id": replacement_action_id,
                "replaces_document_id": replaces_document_id,
                **(
                    {"document_type": replacement_document_type}
                    if replacement_document_type
                    else {}
                ),
            }
        )
        return True

    def _save_document_resume_extraction(
        self,
        claim_id: str,
        document_id: str,
        extraction: DocumentExtractionResult,
    ) -> None:
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={
                "resume_extraction_result": extraction,
                "evidence_facts": (
                    extraction.evidence_facts.fact_values()
                    if extraction.evidence_facts
                    else {}
                ),
                "evidence_findings": extraction.evidence_findings,
            }
        )

    def _mark_document_resume_quality_processed(
        self, claim_id: str, document_id: str
    ) -> None:
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={"resume_quality_processed_at": datetime.now(timezone.utc)}
        )

    def _mark_document_resume_retry_required(
        self, claim_id: str, document_id: str, *, error_type: str
    ) -> None:
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={
                "resume_result_status": "retry_required",
                "resume_error": (
                    f"{error_type}: document resume failed; retry is required."
                ),
                "resume_retry_required_at": datetime.now(timezone.utc),
            }
        )

    def _mark_document_resume_rejected(
        self, claim_id: str, document_id: str, *, error_type: str
    ) -> None:
        self.claim["status"] = "awaiting_documents"
        self.claim.pop("active_resume_document_id", None)
        self.claim.pop("active_resume_idempotency_key", None)
        self.claim.pop("active_resume_correlation_id", None)
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={
                "status": "unusable",
                "resume_processed_at": datetime.now(timezone.utc),
                "resume_result_status": "rejected",
                "resume_error": (
                    f"{error_type}: unsupported document type; retry disabled."
                ),
            }
        )

    def _save_review(self, claim_id: str, review: ReviewResult, **kwargs) -> ClaimStatus:
        status = review_target_status(review)
        self.claim["status"] = status.value
        replacement = kwargs.get("replacement_document")
        if replacement is not None:
            self.documents[replacement.document_id] = replacement
            target = self.documents[replacement.replaces_document_id]
            self.documents[target.document_id] = target.model_copy(
                update={
                    "status": "superseded",
                    "superseded_by_document_id": replacement.document_id,
                }
            )
        document_id = kwargs.get("resume_document_id")
        if document_id:
            self.documents[document_id] = self.documents[document_id].model_copy(
                update={
                    "resume_idempotency_key": kwargs["resume_idempotency_key"],
                    "resume_processed_at": datetime.now(timezone.utc),
                    "resume_result_status": status.value,
                }
            )
            self.claim.pop("active_resume_document_id", None)
            self.claim.pop("active_resume_idempotency_key", None)
            self.claim.pop("active_resume_correlation_id", None)
        return status

    def _complete_requested_evidence_item(
        self,
        *,
        claim_id: str,
        document: ClaimDocument,
        extraction: DocumentExtractionResult,
        remaining_actions,
        idempotency_key: str,
    ) -> None:
        actions = remaining_actions if extraction.usable else [
            action
            for action in parse_requested_actions(self.claim.get("requested_actions", []))
            if isinstance(action, UploadDocumentRequestedAction)
        ]
        self.claim.update({
            "status": "awaiting_documents",
            "requested_actions": [item.model_dump(mode="python") for item in actions],
            "missing_documents": [
                {"type": item.document_type, "reason": item.instruction}
                for item in actions
            ],
        })
        self.claim.pop("active_resume_document_id", None)
        self.claim.pop("active_resume_idempotency_key", None)
        self.claim.pop("active_resume_correlation_id", None)
        self.documents[document.document_id] = document.model_copy(update={
            "status": "validated" if extraction.usable else "unusable",
            "resume_idempotency_key": idempotency_key,
            "resume_processed_at": datetime.now(timezone.utc),
            "resume_result_status": "awaiting_documents",
        })

    def test_retryable_review_failure_resumes_same_replacement_once(self) -> None:
        self.claim["missing_documents"] = [
            {
                "type": "vehicle_identity",
                "reason": "Vehicle identity is required.",
                "source_requirement": "always_required",
            },
            {
                "type": "license_plate_photo",
                "reason": "A readable plate is required.",
                "source_requirement": "license_plate_photo",
            },
        ]
        self.claim["requested_actions"] = [
            {
                "action_id": "ACT-FLOW-3",
                "action_type": "upload_document",
                "review_id": "AUTONOMOUS-FLOW-3",
                "document_type": "damage_evidence",
                "instruction": "Upload corrected identifiable damage evidence.",
                "replaces_document_id": "DOC-WRONG",
            }
        ]
        self.documents["DOC-WRONG"] = ClaimDocument(
            document_id="DOC-WRONG",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="wrong-front.jpg",
            status="validated",
            received_at=datetime.now(timezone.utc),
        )
        replacement = ClaimDocument(
            document_id="DOC-CORRECT",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="correct-rear-plate.jpg",
            storage_path="/demo/correct-rear-plate.jpg",
            requested_action_id="ACT-FLOW-3",
            replaces_document_id="DOC-WRONG",
            received_at=datetime.now(timezone.utc),
        )
        self.documents[replacement.document_id] = replacement
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="Rear damage and a readable plate are visible.",
            supported_capabilities=[
                "damage_evidence",
                "license_plate_photo",
                "vehicle_identity",
            ],
        )
        self.review_service.review.side_effect = [
            ClaimReviewError("Gemini evidence review failed: 429 RESOURCE_EXHAUSTED"),
            review_result(complete=True),
        ]

        with self.assertRaisesRegex(ClaimReviewError, "429 RESOURCE_EXHAUSTED"):
            self.workflow.resume("CLM-A1B2C3D4", replacement)

        self.assertEqual(self.claim["status"], "review_processing")
        result = self.workflow.resume("CLM-A1B2C3D4", replacement)

        self.assertEqual(result.final_status, "inspection_ready")
        self.assertEqual(self.extractor.extract.call_count, 1)
        self.repository.begin_document_resume_review.assert_called_once()
        self.repository.save_document_resume_extraction.assert_called_once()
        self.repository.mark_document_resume_quality_processed.assert_called_once()
        self.repository.add_document.assert_not_called()
        self.repository.save_review_result.assert_called_once()
        replacement_arg = self.repository.save_review_result.call_args.kwargs[
            "replacement_document"
        ]
        self.assertEqual(replacement_arg.replaces_document_id, "DOC-WRONG")
        self.repository.mark_document_superseded.assert_not_called()

    def test_different_document_cannot_join_active_resume_retry(self) -> None:
        active = document(document_id="DOC-ACTIVE")
        active_key = "CLM-A1B2C3D4:DOC-ACTIVE:resume"
        self.documents[active.document_id] = active.model_copy(
            update={
                "resume_idempotency_key": active_key,
                "resume_correlation_id": "corr-active",
                "resume_matched_requirement": "license_plate_photo",
            }
        )
        self.claim.update(
            {
                "status": "review_processing",
                "active_resume_document_id": active.document_id,
                "active_resume_idempotency_key": active_key,
                "active_resume_correlation_id": "corr-active",
            }
        )
        other = document(document_id="DOC-OTHER")
        self.documents[other.document_id] = other

        with self.assertRaisesRegex(
            ClaimResumeError, "same in-progress document resume operation"
        ):
            self.workflow.resume("CLM-A1B2C3D4", other)

        self.extractor.extract.assert_not_called()
        self.review_service.review.assert_not_called()

    def test_valid_missing_document_resumes_to_inspection_ready(self) -> None:
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="The license plate is readable.",
            satisfies_requirement="license_plate_photo",
            evidence_facts=EvidenceArtifactFacts(
                source="untrusted-provider-source",
                license_plate="ABC123",
                damage_location="rear",
                vehicle_make="",
            ),
        )
        self.review_service.review.return_value = review_result(complete=True)
        submitted = document()

        result = self.workflow.resume("CLM-A1B2C3D4", submitted)

        self.assertEqual(result.final_status, "inspection_ready")
        self.assertTrue(result.evidence_usable)
        self.repository.add_document.assert_called_once()
        self.review_service.review.assert_called_once()
        actions = [
            call.kwargs["action"]
            for call in self.repository.append_claim_event.call_args_list
        ]
        self.assertIn("document_received", actions)
        self.assertIn("document_quality_checked", actions)
        self.assertIn("claim_review_resumed", actions)
        self.assertIn("missing_requirement_satisfied", actions)
        self.assertIn("claim_moved_to_inspection_ready", actions)
        persisted = self.documents[submitted.document_id]
        self.assertEqual(
            persisted.evidence_facts,
            {"license_plate": "ABC123", "damage_location": "rear"},
        )
        self.assertIn("damage_location: rear", persisted.evidence_findings)
        self.assertIn("license_plate: ABC123", persisted.evidence_findings)
        self.assertNotIn("vehicle_make", persisted.evidence_facts)

    def test_extraction_failure_persists_recoverable_retry_state(self) -> None:
        submitted = document(document_id="DOC-RETRY")
        self.extractor.extract.side_effect = RuntimeError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            self.workflow.resume("CLM-A1B2C3D4", submitted)

        persisted = self.documents[submitted.document_id]
        self.assertIsNotNone(persisted.resume_started_at)
        self.assertIsNone(persisted.resume_processed_at)
        self.assertEqual(persisted.resume_result_status, "retry_required")
        self.assertIn("RuntimeError", persisted.resume_error or "")
        self.repository.mark_document_resume_retry_required.assert_called_once_with(
            "CLM-A1B2C3D4", submitted.document_id, error_type="RuntimeError"
        )

    def test_unsupported_document_type_is_rejected_without_retry(self) -> None:
        self.claim["missing_documents"] = [
            {
                "type": "unsupported_artifact",
                "reason": "Unsupported test artifact.",
                "source_requirement": "unsupported_artifact",
            }
        ]
        submitted = document(
            document_id="DOC-UNSUPPORTED",
            document_type="unsupported_artifact",
        )
        self.extractor.extract.side_effect = UnsupportedResumeDocumentTypeError(
            "Unsupported resume document type: unsupported_artifact"
        )

        with self.assertRaises(UnsupportedResumeDocumentTypeError):
            self.workflow.resume("CLM-A1B2C3D4", submitted)

        persisted = self.documents[submitted.document_id]
        self.assertEqual(self.claim["status"], "awaiting_documents")
        self.assertEqual(persisted.status, "unusable")
        self.assertIsNotNone(persisted.resume_processed_at)
        self.assertEqual(persisted.resume_result_status, "rejected")
        self.assertIsNone(persisted.resume_retry_required_at)
        self.repository.mark_document_resume_rejected.assert_called_once_with(
            "CLM-A1B2C3D4",
            submitted.document_id,
            error_type="UnsupportedResumeDocumentTypeError",
        )
        self.repository.mark_document_resume_retry_required.assert_not_called()

    def test_adjuster_more_info_upload_action_matches_without_replacement(self) -> None:
        self.claim["missing_documents"] = []
        self.claim["requested_actions"] = [{
            "action_type": "upload_document",
            "action_id": "ACT-MORE-INFO",
            "review_id": "HRV-2",
            "document_type": "damage_evidence",
            "instruction": "Please upload a clearer rear damage photo.",
            "replaces_document_id": None,
        }]

        matched = match_missing_requirement(
            self.claim,
            "damage_evidence",
            requested_action_id="ACT-MORE-INFO",
            replaces_document_id=None,
        )

        self.assertEqual(matched, "damage_evidence")

    def test_adjuster_more_info_upload_resumes_without_replacement_binding(self) -> None:
        self.claim["missing_documents"] = []
        self.claim["requested_actions"] = [{
            "action_type": "upload_document",
            "action_id": "ACT-MORE-INFO",
            "review_id": "HRV-2",
            "document_type": "damage_evidence",
            "instruction": "Please upload a clearer rear damage photo.",
            "replaces_document_id": None,
        }]
        additional = ClaimDocument(
            document_id="DOC-MORE-INFO",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="rear-detail.jpg",
            status="received",
            requested_action_id="ACT-MORE-INFO",
            received_at=datetime.now(timezone.utc),
        )
        self.documents[additional.document_id] = additional
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="The requested rear damage detail is clear.",
            supported_capabilities=["damage_evidence"],
        )
        self.review_service.review.return_value = review_result(complete=True)

        result = self.workflow.resume("CLM-A1B2C3D4", additional)

        self.assertEqual(result.final_status, "inspection_ready")
        begin = self.repository.begin_document_resume_review.call_args.kwargs
        self.assertIsNone(begin["replacement_action_id"])
        saved = self.repository.save_review_result.call_args.kwargs
        self.assertIsNone(saved["replacement_document"])
        self.repository.mark_document_superseded.assert_not_called()

    def test_license_plate_audit_type_alone_does_not_satisfy_identity(self) -> None:
        plate = document().model_copy(update={"status": "validated"})

        metadata = _build_review_metadata(
            claim=self.claim, documents=[plate], conflicts=[]
        )

        self.assertFalse(metadata.vehicle_identity_clear)

    def test_persisted_readable_plate_capability_satisfies_identity(self) -> None:
        plate = document().model_copy(update={
            "status": "validated",
            "supported_capabilities": ["license_plate_photo", "vehicle_identity"],
        })

        metadata = _build_review_metadata(
            claim=self.claim, documents=[plate], conflicts=[]
        )

        self.assertTrue(metadata.vehicle_identity_clear)

    def test_one_clear_plate_satisfies_plate_and_vehicle_identity(self) -> None:
        self.claim["missing_documents"] = [
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
        ]
        self.documents["DOC-DAMAGE"] = ClaimDocument(
            document_id="DOC-DAMAGE",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="damage.jpg",
            storage_path="/demo/damage.jpg",
            status="validated",
            received_at=datetime.now(timezone.utc),
        )
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="The license plate and vehicle identity are readable.",
            satisfies_requirement="license_plate_photo",
        )
        review_client = MagicMock(name="review_client")
        review_client.models.generate_content.return_value.text = review_result(
            complete=True
        ).model_dump_json()
        workflow = ClaimResumeWorkflow(
            repository=self.repository,
            review_service=ClaimReviewService(review_client, "configured-model"),
            document_extractor=self.extractor,
        )

        result = workflow.resume("CLM-A1B2C3D4", document())

        saved_review = self.repository.save_review_result.call_args.args[1]
        self.assertEqual(saved_review.missing_documents, [])
        self.assertEqual(saved_review.unusable_evidence, [])
        self.assertTrue(saved_review.intake_complete)
        self.assertEqual(result.final_status, "inspection_ready")
        self.extractor.extract.assert_called_once()

    def test_followup_image_adds_damage_capability_and_keeps_original_active(
        self,
    ) -> None:
        self.documents["DOC-ORIGINAL"] = ClaimDocument(
            document_id="DOC-ORIGINAL",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="initial-damage.jpg",
            storage_path="/demo/initial-damage.jpg",
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        self.documents["DOC-REPORT"] = ClaimDocument(
            document_id="DOC-REPORT",
            claim_id="CLM-A1B2C3D4",
            document_type="police_report",
            filename="police-report.pdf",
            storage_path="/demo/police-report.pdf",
            received_at=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc),
        )
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="The plate is readable and rear damage is visible.",
            supported_capabilities=["damage_evidence", "license_plate_photo"],
            evidence_findings=[
                "The plate is readable.",
                "Rear damage is visible.",
            ],
        )

        def inspect_review(intake, metadata, *, evidence_parts):
            followup_types = {
                item.evidence_type
                for item in metadata.uploaded_evidence
                if item.filename == "license-plate.jpg"
            }
            self.assertEqual(
                followup_types,
                {"license_plate_photo", "vehicle_identity", "damage_evidence"},
            )
            self.assertIn(
                "initial-damage.jpg",
                {item.filename for item in metadata.uploaded_evidence},
            )
            labels = [part.text for part in evidence_parts if part.text]
            self.assertEqual(
                labels,
                [
                    "Evidence source: license-plate.jpg\nAudit document type: license_plate_photo",
                    "Evidence source: initial-damage.jpg\nAudit document type: damage_evidence",
                    "Evidence source: police-report.pdf\nAudit document type: police_report",
                ],
            )
            return review_result(complete=True)

        self.review_service.review.side_effect = inspect_review
        raw_part = types.Part.from_bytes(data=b"evidence", mime_type="image/jpeg")
        with patch(
            "app.workflows.claim_resume_workflow.evidence_part",
            return_value=raw_part,
        ):
            result = self.workflow.resume("CLM-A1B2C3D4", document())

        self.assertEqual(result.final_status, "inspection_ready")
        self.assertEqual(self.documents["DOC-ORIGINAL"].status, "received")
        self.assertEqual(
            set(self.documents["DOC-12345678"].supported_capabilities),
            {"license_plate_photo", "vehicle_identity", "damage_evidence"},
        )
        self.repository.mark_document_superseded.assert_not_called()

    def test_flow_b_resume_identifies_front_plate_photo_as_damage_outlier(self) -> None:
        self.claim["missing_documents"] = [
            {"type": "vehicle_identity", "reason": "Identity required.",
             "source_requirement": "always_required"},
            {"type": "license_plate_photo", "reason": "Plate required.",
             "source_requirement": "license_plate_photo"},
        ]
        self.documents["DOC-REAR"] = ClaimDocument(
            document_id="DOC-REAR", claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence", filename="original-rear.jpg",
            storage_path="/demo/original-rear.jpg", status="validated",
            supported_capabilities=["damage_evidence"],
            evidence_findings=["Rear damage is visible."],
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        self.documents["DOC-REPORT"] = ClaimDocument(
            document_id="DOC-REPORT", claim_id="CLM-A1B2C3D4",
            document_type="police_report", filename="police-report.pdf",
            storage_path="/demo/police-report.pdf", status="validated",
            evidence_findings=["The report describes rear-end damage."],
            received_at=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc),
        )
        followup = document(document_id="DOC-FRONT")
        followup = followup.model_copy(update={"filename": "followup-front.jpg"})
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True, reason="The plate is readable; front damage is visible.",
            supported_capabilities=["damage_evidence", "license_plate_photo"],
            evidence_findings=["The plate is readable.", "Front-end damage is visible."],
        )
        model_result = review_result(complete=False).model_copy(update={
            "conflicts": [EvidenceConflict(
                field="damage_location", values=["rear", "front"],
                sources=[
                    "police-report.pdf", "original-rear.jpg", "followup-front.jpg"
                ],
                reason="The follow-up image conflicts with corroborated rear damage.",
            )],
            "current_evidence_findings": [
                CurrentEvidenceFinding(
                    source="police-report.pdf", finding="Rear-end damage is reported."
                ),
                CurrentEvidenceFinding(
                    source="original-rear.jpg", finding="Rear damage is visible."
                ),
                CurrentEvidenceFinding(
                    source="followup-front.jpg", finding="Front-end damage is visible."
                ),
            ],
            "operational_indicators": OperationalIndicators(),
        })
        review_client = MagicMock(name="review_client")
        review_client.models.generate_content.return_value.text = (
            model_result.model_dump_json()
        )
        workflow = ClaimResumeWorkflow(
            repository=self.repository,
            review_service=ClaimReviewService(review_client, "configured-model"),
            document_extractor=self.extractor,
        )
        raw_part = types.Part.from_bytes(data=b"evidence", mime_type="image/jpeg")

        with patch(
            "app.workflows.claim_resume_workflow.evidence_part", return_value=raw_part
        ):
            result = workflow.resume("CLM-A1B2C3D4", followup)

        saved = self.repository.save_review_result.call_args.args[1]
        self.assertEqual(result.final_status, "awaiting_documents")
        self.assertEqual(saved.requested_actions[0].action_type, "upload_document")
        self.assertEqual(saved.missing_documents, [])
        self.assertEqual(
            saved.source_aware_conflicts[0].selected_outlier_document_id,
            "DOC-FRONT",
        )
        self.assertEqual(
            set(self.documents["DOC-FRONT"].supported_capabilities),
            {"damage_evidence", "license_plate_photo", "vehicle_identity"},
        )
        self.assertEqual(self.documents["DOC-REAR"].status, "validated")
        self.repository.mark_document_superseded.assert_not_called()

    def test_resolved_historical_uncertainties_do_not_escalate_resumed_review(
        self,
    ) -> None:
        self.claim["uncertainties"] = [
            "Vehicle identity was unavailable.",
            "The plate was not visible.",
            "The vehicle identifier could not be confirmed.",
        ]
        self.documents["DOC-DAMAGE"] = ClaimDocument(
            document_id="DOC-DAMAGE",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="damage.jpg",
            storage_path="/demo/damage.jpg",
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="The plate and vehicle identity are readable.",
            evidence_findings=["The plate is readable."],
        )
        review_client = MagicMock(name="review_client")
        review_client.models.generate_content.return_value.text = review_result(
            complete=True
        ).model_dump_json()
        workflow = ClaimResumeWorkflow(
            repository=self.repository,
            review_service=ClaimReviewService(review_client, "configured-model"),
            document_extractor=self.extractor,
        )

        result = workflow.resume("CLM-A1B2C3D4", document())

        saved_review = self.repository.save_review_result.call_args.args[1]
        self.assertEqual(saved_review.unresolved_uncertainties, [])
        self.assertFalse(saved_review.requires_human_review)
        self.assertEqual(result.final_status, "inspection_ready")

    def test_explicit_replacement_supersedes_only_named_artifact(self) -> None:
        self.claim["requested_actions"] = [
            {
                "action_id": "ACT-REPLACE",
                "action_type": "upload_document",
                "review_id": "HRV-1",
                "document_type": "license_plate_photo",
                "instruction": "Upload a replacement.",
                "replaces_document_id": "DOC-OLD",
            }
        ]
        self.documents["DOC-OLD"] = ClaimDocument(
            document_id="DOC-OLD",
            claim_id="CLM-A1B2C3D4",
            document_type="license_plate_photo",
            filename="old-blurry.jpg",
            storage_path="/demo/old-blurry.jpg",
            status="unusable",
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        self.documents["DOC-OTHER"] = ClaimDocument(
            document_id="DOC-OTHER",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="damage.jpg",
            storage_path="/demo/damage.jpg",
            received_at=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc),
        )
        replacement = document().model_copy(
            update={
                "replaces_document_id": "DOC-OLD",
                "requested_action_id": "ACT-REPLACE",
            }
        )
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="The replacement plate is readable.",
        )
        self.review_service.review.return_value = review_result(complete=True)

        self.workflow.resume("CLM-A1B2C3D4", replacement)

        save_kwargs = self.repository.save_review_result.call_args.kwargs
        self.assertEqual(
            save_kwargs["review_generation_key"],
            "CLM-A1B2C3D4:DOC-12345678:resume",
        )
        replacement_document = save_kwargs["replacement_document"]
        self.assertEqual(replacement_document.replaces_document_id, "DOC-OLD")
        self.assertEqual(replacement_document.status, "validated")
        self.repository.mark_document_unusable.assert_not_called()
        self.repository.mark_document_validated.assert_not_called()
        self.repository.mark_document_superseded.assert_not_called()

    def test_policy_replacement_uses_same_atomic_supersession_path(self) -> None:
        self.claim["requested_actions"] = [
            {
                "action_id": "ACT-POLICY-REPLACE",
                "action_type": "upload_document",
                "review_id": "AUTONOMOUS-POLICY",
                "document_type": "policy_document",
                "instruction": "Upload the correct policy document.",
                "replaces_document_id": "DOC-OLD-POLICY",
            }
        ]
        self.documents["DOC-OLD-POLICY"] = ClaimDocument(
            document_id="DOC-OLD-POLICY",
            claim_id="CLM-A1B2C3D4",
            document_type="policy_document",
            filename="old-policy.pdf",
            status="validated",
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        replacement = ClaimDocument(
            document_id="DOC-NEW-POLICY",
            claim_id="CLM-A1B2C3D4",
            document_type="policy_document",
            filename="correct-policy.pdf",
            status="received",
            requested_action_id="ACT-POLICY-REPLACE",
            replaces_document_id="DOC-OLD-POLICY",
            received_at=datetime.now(timezone.utc),
        )
        self.documents[replacement.document_id] = replacement
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="The replacement policy document is readable.",
            evidence_facts=EvidenceArtifactFacts(
                source="correct-policy.pdf", policy_number="POL-123"
            ),
        )
        self.review_service.review.return_value = review_result(complete=True)

        result = self.workflow.resume("CLM-A1B2C3D4", replacement)

        self.assertEqual(result.final_status, "inspection_ready")
        self.assertEqual(self.documents["DOC-OLD-POLICY"].status, "superseded")
        self.assertEqual(
            self.documents["DOC-OLD-POLICY"].superseded_by_document_id,
            "DOC-NEW-POLICY",
        )
        persisted = self.documents["DOC-NEW-POLICY"]
        self.assertEqual(persisted.replaces_document_id, "DOC-OLD-POLICY")
        self.assertEqual(persisted.requested_action_id, "ACT-POLICY-REPLACE")
        self.assertEqual(persisted.status, "validated")

    def test_partial_multi_item_fulfillment_keeps_remaining_action(self) -> None:
        self.claim["missing_documents"] = [
            {"type": "policy_document", "reason": "Upload policy."},
            {"type": "police_report", "reason": "Upload report."},
        ]
        self.claim["requested_actions"] = [
            {
                "action_id": "ACT-POLICY",
                "action_type": "upload_document",
                "review_id": "HRV-MULTI",
                "document_type": "policy_document",
                "instruction": "Upload policy.",
            },
            {
                "action_id": "ACT-REPORT",
                "action_type": "upload_document",
                "review_id": "HRV-MULTI",
                "document_type": "police_report",
                "instruction": "Upload report.",
            },
        ]
        submitted = ClaimDocument(
            document_id="DOC-POLICY",
            claim_id="CLM-A1B2C3D4",
            document_type="policy_document",
            filename="policy.pdf",
            requested_action_id="ACT-POLICY",
            received_at=datetime.now(timezone.utc),
        )
        self.documents[submitted.document_id] = submitted
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="Policy is readable.",
            evidence_facts=EvidenceArtifactFacts(
                source="policy.pdf", policy_number="POL-123"
            ),
        )

        result = self.workflow.resume("CLM-A1B2C3D4", submitted)

        self.assertEqual(result.final_status, "awaiting_documents")
        self.assertTrue(result.evidence_usable)
        remaining = self.repository.complete_requested_evidence_item.call_args.kwargs[
            "remaining_actions"
        ]
        self.assertEqual([item.action_id for item in remaining], ["ACT-REPORT"])
        self.review_service.review.assert_not_called()
        actions = [
            call.kwargs["action"]
            for call in self.repository.append_claim_event.call_args_list
        ]
        self.assertNotIn("claim_review_resumed", actions)

    def test_all_multi_item_requirements_resume_review_once(self) -> None:
        self.claim["missing_documents"] = [
            {"type": "policy_document", "reason": "Upload policy."},
            {"type": "police_report", "reason": "Upload report."},
        ]
        self.claim["requested_actions"] = [
            {
                "action_id": "ACT-POLICY", "action_type": "upload_document",
                "review_id": "HRV-MULTI", "document_type": "policy_document",
                "instruction": "Upload policy.",
            },
            {
                "action_id": "ACT-REPORT", "action_type": "upload_document",
                "review_id": "HRV-MULTI", "document_type": "police_report",
                "instruction": "Upload report.",
            },
        ]
        policy = ClaimDocument(
            document_id="DOC-POLICY", claim_id="CLM-A1B2C3D4",
            document_type="policy_document", filename="policy.pdf",
            requested_action_id="ACT-POLICY", received_at=datetime.now(timezone.utc),
        )
        report = ClaimDocument(
            document_id="DOC-REPORT", claim_id="CLM-A1B2C3D4",
            document_type="police_report", filename="report.pdf",
            requested_action_id="ACT-REPORT", received_at=datetime.now(timezone.utc),
        )
        self.documents.update({policy.document_id: policy, report.document_id: report})
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True, reason="Readable evidence."
        )
        self.review_service.review.return_value = review_result(complete=True)

        first = self.workflow.resume("CLM-A1B2C3D4", policy)
        second = self.workflow.resume("CLM-A1B2C3D4", report)

        self.assertEqual(first.final_status, "awaiting_documents")
        self.assertEqual(second.final_status, "inspection_ready")
        self.review_service.review.assert_called_once()
        resumed = [
            call for call in self.repository.append_claim_event.call_args_list
            if call.kwargs["action"] == "claim_review_resumed"
        ]
        self.assertEqual(len(resumed), 1)

        replay = self.workflow.resume("CLM-A1B2C3D4", report)
        self.assertTrue(replay.idempotent_replay)
        self.review_service.review.assert_called_once()

    def test_unusable_multi_item_upload_leaves_all_requests_open(self) -> None:
        self.claim["missing_documents"] = [
            {"type": "policy_document", "reason": "Upload policy."},
            {"type": "police_report", "reason": "Upload report."},
        ]
        self.claim["requested_actions"] = [
            {
                "action_id": "ACT-POLICY", "action_type": "upload_document",
                "review_id": "HRV-MULTI", "document_type": "policy_document",
                "instruction": "Upload policy.",
            },
            {
                "action_id": "ACT-REPORT", "action_type": "upload_document",
                "review_id": "HRV-MULTI", "document_type": "police_report",
                "instruction": "Upload report.",
            },
        ]
        submitted = ClaimDocument(
            document_id="DOC-BLURRY", claim_id="CLM-A1B2C3D4",
            document_type="policy_document", filename="blurry.pdf",
            requested_action_id="ACT-POLICY", received_at=datetime.now(timezone.utc),
        )
        self.documents[submitted.document_id] = submitted
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=False, reason="The document is unreadable."
        )

        result = self.workflow.resume("CLM-A1B2C3D4", submitted)

        self.assertEqual(result.final_status, "awaiting_documents")
        self.assertFalse(result.evidence_usable)
        self.assertEqual(len(self.claim["requested_actions"]), 2)
        self.review_service.review.assert_not_called()

    def test_current_context_uses_active_replacement_facts_not_superseded_facts(
        self,
    ) -> None:
        old = ClaimDocument(
            document_id="DOC-OLD", claim_id="CLM-A1B2C3D4",
            document_type="policy_document", filename="old.pdf",
            status="superseded", evidence_facts={"policy_number": "POL-OLD"},
            received_at=datetime.now(timezone.utc),
        )
        replacement = ClaimDocument(
            document_id="DOC-NEW", claim_id="CLM-A1B2C3D4",
            document_type="policy_document", filename="new.pdf",
            status="validated", replaces_document_id="DOC-OLD",
            evidence_facts={"policy_number": "POL-NEW"},
            evidence_findings=["policy_number: POL-NEW"],
            received_at=datetime.now(timezone.utc),
        )

        intake = _current_review_intake_result(self.claim, [old, replacement])

        self.assertEqual(intake.policy_number, "POL-NEW")
        self.assertNotIn("POL-OLD", intake.model_dump_json())

    def test_claimant_fact_is_preserved_as_explicit_current_evidence_conflict(self) -> None:
        self.claim["policy_number_hint"] = "POL-CLAIMANT"
        replacement = ClaimDocument(
            document_id="DOC-NEW", claim_id="CLM-A1B2C3D4",
            document_type="policy_document", filename="new.pdf",
            status="validated", evidence_facts={"policy_number": "POL-EVIDENCE"},
            received_at=datetime.now(timezone.utc),
        )

        intake = _current_review_intake_result(self.claim, [replacement])
        metadata = _build_review_metadata(
            claim=self.claim, documents=[replacement], conflicts=[]
        )

        self.assertEqual(intake.policy_number, "POL-EVIDENCE")
        conflict = next(
            item for item in metadata.known_conflicts
            if item.field == "policy_number"
        )
        self.assertEqual(conflict.values, ["POL-CLAIMANT", "POL-EVIDENCE"])
        self.assertEqual(
            conflict.sources, ["claimant submission", "current active evidence"]
        )

    def test_flow_4_correct_rear_plate_replacement_reaches_inspection(self) -> None:
        self.claim["missing_documents"] = []
        self.claim["requested_actions"] = [{
            "action_id": "ACT-FLOW-4", "action_type": "upload_document",
            "review_id": "HRV-FLOW-4", "document_type": "damage_evidence",
            "instruction": "Please upload the correct damage photo for this claim.",
            "replaces_document_id": "DOC-FRONT",
        }]
        self.documents["DOC-FRONT"] = ClaimDocument(
            document_id="DOC-FRONT", claim_id="CLM-A1B2C3D4",
            document_type="license_plate_photo", filename="vehicle_damage_front.jpg",
            status="unusable",
            supported_capabilities=["damage_evidence", "vehicle_identity"],
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        replacement = ClaimDocument(
            document_id="DOC-CORRECT", claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence", filename="correct-rear-plate.jpg",
            requested_action_id="ACT-FLOW-4", replaces_document_id="DOC-FRONT",
            received_at=datetime.now(timezone.utc),
        )
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="Rear damage and a readable plate are visible.",
            supported_capabilities=["damage_evidence", "license_plate_photo"],
            evidence_findings=["Rear damage is visible.", "The plate is readable."],
        )

        def review_current_evidence(intake, metadata, *, evidence_parts):
            by_filename = {
                item.filename: item for item in metadata.uploaded_evidence
            }
            self.assertNotIn("vehicle_damage_front.jpg", by_filename)
            self.assertEqual(
                {
                    item.evidence_type
                    for item in metadata.uploaded_evidence
                    if item.filename == "correct-rear-plate.jpg"
                },
                {"damage_evidence", "license_plate_photo", "vehicle_identity"},
            )
            self.assertTrue(metadata.vehicle_identity_clear)
            return review_result(complete=True)

        self.review_service.review.side_effect = review_current_evidence

        result = self.workflow.resume("CLM-A1B2C3D4", replacement)

        committed = self.repository.save_review_result.call_args.kwargs[
            "replacement_document"
        ]
        self.assertEqual(result.final_status, "inspection_ready")
        self.assertEqual(committed.replaces_document_id, "DOC-FRONT")
        self.assertEqual(committed.status, "validated")

    def test_autonomous_replacement_without_client_action_id_supersedes_wrong_photo(
        self,
    ) -> None:
        self.claim["missing_documents"] = [
            {
                "type": "vehicle_identity",
                "reason": "Vehicle identity is required.",
                "source_requirement": "always_required",
            },
            {
                "type": "license_plate_photo",
                "reason": "A readable plate is required.",
                "source_requirement": "license_plate_photo",
            },
        ]
        self.claim["requested_actions"] = [{
            "action_id": "ACT-AUTONOMOUS-FLOW-3",
            "action_type": "upload_document",
            "review_id": "AUTONOMOUS-FLOW-3",
            "document_type": "damage_evidence",
            "instruction": "Upload correct rear damage with a readable plate.",
            "replaces_document_id": "DOC-FRONT",
        }]
        self.documents["DOC-REPORT"] = ClaimDocument(
            document_id="DOC-REPORT", claim_id="CLM-A1B2C3D4",
            document_type="police_report", filename="police-report.pdf",
            storage_path="/demo/police-report.pdf", status="received",
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        self.documents["DOC-FRONT"] = ClaimDocument(
            document_id="DOC-FRONT", claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence", filename="vehicle_damage_front.jpg",
            storage_path="/demo/vehicle_damage_front.jpg", status="validated",
            supported_capabilities=["damage_evidence"],
            evidence_findings=[
                "Silver SUV with front damage and steam from the radiator."
            ], received_at=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc),
        )
        replacement = ClaimDocument(
            document_id="DOC-CORRECT", claim_id="CLM-A1B2C3D4",
            document_type="license_plate_photo",
            filename="vehicle_damage_license.jpg",
            storage_path="/demo/vehicle_damage_license.jpg", status="received",
            received_at=datetime.now(timezone.utc),
        )
        self.documents[replacement.document_id] = replacement
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="Rear damage and plate 7ABX123 are readable.",
            supported_capabilities=[
                "damage_evidence", "license_plate_photo", "vehicle_identity"
            ],
            evidence_findings=[
                "Dark grey sedan with rear damage and readable plate 7ABX123."
            ],
            evidence_facts=EvidenceArtifactFacts(
                source="provider-source-is-overridden",
                vehicle_identity="dark grey sedan",
                license_plate="7ABX123",
                damage_location="rear",
            ),
        )

        def review_active_only(intake, metadata, *, evidence_parts):
            sources = {item.filename for item in metadata.uploaded_evidence}
            self.assertNotIn("vehicle_damage_front.jpg", sources)
            self.assertIn("vehicle_damage_license.jpg", sources)
            self.assertTrue(metadata.vehicle_identity_clear)
            evidence_text = " ".join(
                part.text or "" for part in evidence_parts if part.text is not None
            )
            self.assertNotIn("vehicle_damage_front.jpg", evidence_text)
            self.assertIn("vehicle_damage_license.jpg", evidence_text)
            return review_result(complete=True)

        self.review_service.review.side_effect = review_active_only

        raw_part = types.Part.from_bytes(data=b"evidence", mime_type="image/jpeg")
        with patch(
            "app.workflows.claim_resume_workflow.evidence_part",
            return_value=raw_part,
        ):
            result = self.workflow.resume("CLM-A1B2C3D4", replacement)

        self.assertEqual(result.final_status, "inspection_ready")
        saved = self.repository.save_review_result.call_args.args[1]
        self.assertFalse(saved.requires_human_review)
        self.assertFalse(saved.operational_indicators.safety_concern)
        self.assertEqual(self.documents["DOC-FRONT"].status, "superseded")
        self.assertEqual(
            self.documents["DOC-FRONT"].superseded_by_document_id, "DOC-CORRECT"
        )
        self.assertEqual(
            self.documents["DOC-CORRECT"].replaces_document_id, "DOC-FRONT"
        )
        self.assertEqual(
            self.documents["DOC-CORRECT"].evidence_facts,
            {
                "vehicle_identity": "dark grey sedan",
                "license_plate": "7ABX123",
                "damage_location": "rear",
            },
        )
        self.assertIn(
            "damage_location: rear",
            self.documents["DOC-CORRECT"].evidence_findings,
        )
        begin = self.repository.begin_document_resume_review.call_args.kwargs
        self.assertEqual(begin["replacement_action_id"], "ACT-AUTONOMOUS-FLOW-3")
        self.assertEqual(begin["replaces_document_id"], "DOC-FRONT")

        replay = self.workflow.resume("CLM-A1B2C3D4", replacement)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(self.repository.save_review_result.call_count, 1)

    def test_replacement_does_not_preserve_identity_from_superseded_target(self) -> None:
        self.claim["missing_documents"] = []
        self.claim["requested_actions"] = [{
            "action_id": "ACT-REPLACE", "action_type": "upload_document",
            "review_id": "HRV-1", "document_type": "damage_evidence",
            "instruction": "Upload the correct damage photo.",
            "replaces_document_id": "DOC-FRONT",
        }]
        self.documents["DOC-FRONT"] = ClaimDocument(
            document_id="DOC-FRONT", claim_id="CLM-A1B2C3D4",
            document_type="license_plate_photo", filename="front-plate.jpg",
            status="validated",
            supported_capabilities=[
                "license_plate_photo", "vehicle_identity", "damage_evidence"
            ],
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        self.documents["DOC-REAR"] = ClaimDocument(
            document_id="DOC-REAR", claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence", filename="rear.jpg",
            status="validated", supported_capabilities=["damage_evidence"],
            received_at=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc),
        )
        replacement = ClaimDocument(
            document_id="DOC-NEW", claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence", filename="correct-rear.jpg",
            status="received", requested_action_id="ACT-REPLACE",
            replaces_document_id="DOC-FRONT",
            received_at=datetime.now(timezone.utc),
        )
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True, reason="Rear damage is visible.",
            supported_capabilities=["damage_evidence"],
            evidence_findings=["Rear damage is visible."],
        )
        review_client = MagicMock(name="review_client")
        review_client.models.generate_content.return_value.text = review_result(
            complete=False
        ).model_dump_json()
        workflow = ClaimResumeWorkflow(
            repository=self.repository,
            review_service=ClaimReviewService(review_client, "configured-model"),
            document_extractor=self.extractor,
        )

        result = workflow.resume("CLM-A1B2C3D4", replacement)

        saved = self.repository.save_review_result.call_args.args[1]
        self.assertEqual(result.final_status, "awaiting_documents")
        self.assertIn("vehicle_identity", {item.type for item in saved.missing_documents})
        self.assertEqual(
            self.repository.save_review_result.call_args.kwargs[
                "replacement_document"
            ].replaces_document_id,
            "DOC-FRONT",
        )

    def test_unusable_implicit_replacement_keeps_target_and_action_retryable(
        self,
    ) -> None:
        self.claim["requested_actions"] = [
            {
                "action_id": "ACT-REPLACE",
                "action_type": "upload_document",
                "review_id": "HRV-1",
                "document_type": "damage_evidence",
                "instruction": "Upload the correct damage photo.",
                "replaces_document_id": "DOC-OLD",
            }
        ]
        self.claim["missing_documents"] = []
        self.claim["unusable_evidence"] = []
        self.documents["DOC-OLD"] = ClaimDocument(
            document_id="DOC-OLD",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="initial.jpg",
            status="received",
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        replacement = ClaimDocument(
            document_id="DOC-NEW",
            claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence",
            filename="replacement.jpg",
            status="received",
            received_at=datetime.now(timezone.utc),
        )
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=False,
            reason="The replacement image is too blurry.",
        )
        self.review_service.review.return_value = review_result(complete=False)

        result = self.workflow.resume("CLM-A1B2C3D4", replacement)

        save_kwargs = self.repository.save_review_result.call_args.kwargs
        self.assertEqual(result.final_status, "awaiting_documents")
        self.assertIsNone(save_kwargs["replacement_document"])
        self.assertEqual(
            save_kwargs["retry_replacement_action_id"], "ACT-REPLACE"
        )
        self.assertEqual(self.documents["DOC-OLD"].status, "received")

    def test_unrelated_ordinary_upload_does_not_join_replacement_action(self) -> None:
        self.claim["missing_documents"] = [{
            "type": "police_report",
            "reason": "The police report is required.",
            "source_requirement": "police_report",
        }]
        self.claim["requested_actions"] = [{
            "action_id": "ACT-REPLACE",
            "action_type": "upload_document",
            "review_id": "AUTONOMOUS-1",
            "document_type": "damage_evidence",
            "instruction": "Upload the correct damage photo.",
            "replaces_document_id": "DOC-OLD",
        }]
        self.documents["DOC-OLD"] = ClaimDocument(
            document_id="DOC-OLD", claim_id="CLM-A1B2C3D4",
            document_type="damage_evidence", filename="wrong.jpg",
            status="validated", received_at=datetime.now(timezone.utc),
        )
        report = ClaimDocument(
            document_id="DOC-REPORT", claim_id="CLM-A1B2C3D4",
            document_type="police_report", filename="report.pdf",
            status="received", received_at=datetime.now(timezone.utc),
        )
        self.documents[report.document_id] = report
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True, reason="The police report is readable."
        )
        self.review_service.review.return_value = review_result(complete=False)

        self.workflow.resume("CLM-A1B2C3D4", report)

        kwargs = self.repository.save_review_result.call_args.kwargs
        self.assertIsNone(kwargs["replacement_document"])
        self.assertEqual(self.documents["DOC-OLD"].status, "validated")
        begin = self.repository.begin_document_resume_review.call_args.kwargs
        self.assertIsNone(begin["replacement_action_id"])

    def test_blurry_document_stays_awaiting_documents(self) -> None:
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=False,
            reason="The license plate cannot be verified.",
        )
        self.review_service.review.return_value = review_result(complete=False)

        result = self.workflow.resume("CLM-A1B2C3D4", document())

        self.assertEqual(result.final_status, "awaiting_documents")
        self.assertFalse(result.evidence_usable)
        self.repository.mark_document_unusable.assert_called_once()

    def test_medical_attachment_preserves_injury_human_review_boundary(self) -> None:
        self.claim.update({
            "operational_indicators": {"possible_injury": True},
            "missing_documents": [],
            "requested_actions": [{
                "action_id": "ACT-MEDICAL",
                "action_type": "upload_document",
                "review_id": "HRV-INJURY",
                "document_type": "medical_document",
                "instruction": "Please upload medical documentation.",
                "replaces_document_id": None,
            }],
        })
        medical = document(
            document_id="DOC-MEDICAL", document_type="medical_document"
        ).model_copy(update={
            "filename": "medical-record.pdf",
            "requested_action_id": "ACT-MEDICAL",
        })
        self.documents[medical.document_id] = medical
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="Medical documentation was received for human review.",
            satisfies_requirement="medical_document",
        )
        self.review_service.review.return_value = review_result(
            complete=False, human_review=True
        )

        result = self.workflow.resume("CLM-A1B2C3D4", medical)

        metadata = self.review_service.review.call_args.args[1]
        self.assertTrue(metadata.injury_mentioned)
        self.assertEqual(
            self.review_service.review.call_args.kwargs["evidence_parts"], []
        )
        self.assertEqual(result.final_status, "human_review_required")
        saved = self.documents[medical.document_id]
        self.assertEqual(saved.status, "validated")
        self.assertEqual(saved.evidence_findings, [])
        self.assertEqual(saved.evidence_facts, {})

    def test_medical_resume_cannot_downgrade_durable_injury_signal(self) -> None:
        self.claim.update({
            "operational_indicators": {"possible_injury": True},
            "missing_documents": [],
            "requested_actions": [{
                "action_id": "ACT-MEDICAL",
                "action_type": "upload_document",
                "review_id": "HRV-INJURY",
                "document_type": "medical_document",
                "instruction": (
                    "Please upload medical documentation related to the "
                    "reported injury."
                ),
                "replaces_document_id": None,
            }],
        })
        medical = document(
            document_id="DOC-MEDICAL", document_type="medical_document"
        ).model_copy(update={
            "filename": "medical-record.pdf",
            "requested_action_id": "ACT-MEDICAL",
        })
        self.documents[medical.document_id] = medical
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="Medical documentation was received for human review.",
            satisfies_requirement="medical_document",
        )
        provider = MagicMock(name="gemini_client")
        provider.models.generate_content.return_value.text = ReviewResult(
            intake_complete=True,
            intake_priority="routine",
            priority_reason="No urgent indicator.",
            confidence=0.9,
            inspection_required=True,
            requires_human_review=False,
            operational_indicators=OperationalIndicators(
                possible_injury=False
            ),
            review_outcome="requires_human_judgment",
            recommended_next_step="human_review",
        ).model_dump_json()
        self.workflow._review_service = ClaimReviewService(
            provider, "configured-model-id"
        )

        result = self.workflow.resume("CLM-A1B2C3D4", medical)

        persisted_review = self.repository.save_review_result.call_args.args[1]
        self.assertEqual(result.final_status, "human_review_required")
        self.assertTrue(persisted_review.operational_indicators.possible_injury)
        self.assertTrue(persisted_review.requires_human_review)
        self.assertEqual(
            review_target_status(persisted_review),
            ClaimStatus.HUMAN_REVIEW_REQUIRED,
        )
        self.assertEqual(
            self.workflow._review_service._client.models.generate_content.call_count,
            1,
        )
        self.assertEqual(self.documents[medical.document_id].evidence_findings, [])
        self.assertEqual(self.documents[medical.document_id].evidence_facts, {})

    def test_unrelated_document_does_not_start_downstream_review(self) -> None:
        unrelated = document(document_type="repair_estimate")

        result = self.workflow.resume("CLM-A1B2C3D4", unrelated)

        self.assertEqual(result.final_status, "awaiting_documents")
        self.assertIsNone(result.matched_requirement)
        self.extractor.extract.assert_not_called()
        self.review_service.review.assert_not_called()
        self.repository.update_claim_status.assert_not_called()

    def test_conflicting_new_document_routes_to_human_review(self) -> None:
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["ABC123", "XYZ789"],
            sources=["police-report.pdf", "license-plate.jpg"],
            reason="The plate values conflict.",
        )
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="The plate is readable but conflicts with prior evidence.",
            conflicts=[conflict],
        )

        def review_with_conflict(intake, metadata, **kwargs):
            self.assertEqual(metadata.known_conflicts, [conflict])
            return review_result(complete=False, human_review=True)

        self.review_service.review.side_effect = review_with_conflict

        result = self.workflow.resume("CLM-A1B2C3D4", document())

        self.assertEqual(result.final_status, "human_review_required")

    def test_duplicate_document_resume_is_idempotent(self) -> None:
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True, reason="The plate is readable."
        )
        self.review_service.review.return_value = review_result(complete=True)
        new_document = document()

        first = self.workflow.resume("CLM-A1B2C3D4", new_document)
        event_count = len(self.repository.append_claim_event.call_args_list)
        second = self.workflow.resume("CLM-A1B2C3D4", new_document)

        self.assertFalse(first.idempotent_replay)
        self.assertTrue(second.idempotent_replay)
        self.repository.add_document.assert_called_once()
        self.assertEqual(self.review_service.review.call_count, 1)
        self.assertEqual(
            len(self.repository.append_claim_event.call_args_list), event_count
        )

    def test_invalid_current_state_is_rejected(self) -> None:
        self.claim["status"] = "inspection_pending"

        with self.assertRaisesRegex(ClaimResumeError, "expected awaiting_documents"):
            self.workflow.resume("CLM-A1B2C3D4", document())

        self.repository.add_document.assert_not_called()
        self.extractor.extract.assert_not_called()

    def test_firestore_write_failure_is_surfaced(self) -> None:
        self.repository.add_document.side_effect = FirestoreWriteError(
            "Could not add claim document"
        )

        with self.assertRaises(FirestoreWriteError):
            self.workflow.resume("CLM-A1B2C3D4", document())


if __name__ == "__main__":
    unittest.main()
