import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.domain.claim_status import ClaimStatus, review_target_status
from app.models.claim_document import ClaimDocument, DocumentExtractionResult
from app.models.review_result import EvidenceConflict, ReviewResult
from app.services.claim_review_service import ClaimReviewService
from app.tools.firestore_repository import FirestoreWriteError
from app.workflows.claim_resume_workflow import ClaimResumeError, ClaimResumeWorkflow


def awaiting_claim() -> dict[str, object]:
    return {
        "claim_id": "CLM-A1B2C3D4",
        "status": "awaiting_documents",
        "claim_type": "auto_collision",
        "damage_type": "Rear bumper damage",
        "parts_affected": ["rear bumper"],
        "incident_summary": "The vehicle was struck from behind.",
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
        self.repository.update_claim_status.side_effect = self._update_status
        self.repository.save_review_result.side_effect = self._save_review
        self.workflow = ClaimResumeWorkflow(
            repository=self.repository,
            review_service=self.review_service,
            document_extractor=self.extractor,
        )

    def _add_document(self, value: ClaimDocument) -> None:
        self.documents[value.document_id] = value

    def _mark_validated(self, claim_id: str, document_id: str) -> None:
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={"status": "validated"}
        )

    def _mark_unusable(
        self, claim_id: str, document_id: str, reason: str
    ) -> None:
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={"status": "unusable", "quality_reason": reason}
        )

    def _update_status(self, claim_id: str, status: ClaimStatus) -> ClaimStatus:
        self.claim["status"] = ClaimStatus(status).value
        return ClaimStatus(status)

    def _save_review(self, claim_id: str, review: ReviewResult, **kwargs) -> ClaimStatus:
        status = review_target_status(review)
        self.claim["status"] = status.value
        document_id = kwargs.get("resume_document_id")
        if document_id:
            self.documents[document_id] = self.documents[document_id].model_copy(
                update={
                    "resume_idempotency_key": kwargs["resume_idempotency_key"],
                    "resume_processed_at": datetime.now(timezone.utc),
                    "resume_result_status": status.value,
                }
            )
        return status

    def test_valid_missing_document_resumes_to_inspection_pending(self) -> None:
        self.extractor.extract.return_value = DocumentExtractionResult(
            usable=True,
            reason="The license plate is readable.",
            satisfies_requirement="license_plate_photo",
        )
        self.review_service.review.return_value = review_result(complete=True)

        result = self.workflow.resume("CLM-A1B2C3D4", document())

        self.assertEqual(result.final_status, "inspection_pending")
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
        self.assertIn("claim_moved_to_inspection_pending", actions)

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
        self.assertEqual(result.final_status, "inspection_pending")
        self.extractor.extract.assert_called_once()

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

        def review_with_conflict(intake, metadata):
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
