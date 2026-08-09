import unittest
from datetime import datetime, timezone

from app.domain.evidence_reasoning import shape_source_aware_conflicts
from app.models.claim_document import ClaimDocument
from app.models.requested_action import UploadDocumentRequestedAction
from app.models.review_result import (
    ClaimEvidenceMetadata,
    CurrentEvidenceFinding,
    EvidenceConflict,
    OperationalIndicators,
    ReviewResult,
    UploadedEvidence,
)
from app.workflows.claim_resume_workflow import (
    _reconcile_current_document_as_replacement,
)


NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


def current_document(*, capabilities: tuple[str, ...]) -> ClaimDocument:
    return ClaimDocument(
        document_id="DOC-CURRENT",
        claim_id="CLM-FLOW-4",
        document_type="license_plate_photo",
        filename="correct-rear-plate.jpg",
        status="validated",
        supported_capabilities=list(capabilities),
        evidence_findings=["The identified grey sedan has rear damage."],
        received_at=NOW,
    )


def metadata(*, include_report: bool = True) -> ClaimEvidenceMetadata:
    uploaded = [
        UploadedEvidence(
            evidence_type="damage_evidence",
            filename="bad-front.jpg",
            document_id="DOC-BAD",
            source_identity="document:DOC-BAD",
            document_type="license_plate_photo",
            usable=True,
            evidence_findings=["A different silver SUV has front damage."],
        ),
        UploadedEvidence(
            evidence_type="damage_evidence",
            filename="correct-rear-plate.jpg",
            document_id="DOC-CURRENT",
            source_identity="document:DOC-CURRENT",
            document_type="license_plate_photo",
            usable=True,
            evidence_findings=["The identified grey sedan has rear damage."],
        ),
        UploadedEvidence(
            evidence_type="vehicle_identity",
            filename="correct-rear-plate.jpg",
            document_id="DOC-CURRENT",
            source_identity="document:DOC-CURRENT",
            document_type="license_plate_photo",
            usable=True,
        ),
        UploadedEvidence(
            evidence_type="license_plate_photo",
            filename="correct-rear-plate.jpg",
            document_id="DOC-CURRENT",
            source_identity="document:DOC-CURRENT",
            document_type="license_plate_photo",
            usable=True,
        ),
    ]
    if include_report:
        uploaded.append(UploadedEvidence(
            evidence_type="police_report",
            filename="police-report.pdf",
            document_id="DOC-REPORT",
            source_identity="document:DOC-REPORT",
            document_type="police_report",
            usable=True,
            evidence_findings=["Rear-end collision with rear damage."],
        ))
    return ClaimEvidenceMetadata(
        uploaded_evidence=uploaded,
        vehicle_identity_clear=True,
    )


def conflicted_review(value: ClaimEvidenceMetadata) -> ReviewResult:
    conflict = EvidenceConflict(
        field="damage_location",
        values=["rear", "front", "rear"],
        sources=[
            "police-report.pdf",
            "bad-front.jpg",
            "correct-rear-plate.jpg",
        ],
        reason="One image conflicts with the corroborated rear-damage evidence.",
    )
    findings = [
        CurrentEvidenceFinding(
            source="police-report.pdf", finding="Rear-end collision with rear damage."
        ),
        CurrentEvidenceFinding(
            source="bad-front.jpg", finding="A different silver SUV has front damage."
        ),
        CurrentEvidenceFinding(
            source="correct-rear-plate.jpg",
            finding="The identified grey sedan has rear damage.",
        ),
    ]
    return ReviewResult(
        intake_complete=False,
        intake_priority="expedited",
        priority_reason="Claimant evidence can resolve the discrepancy.",
        confidence=0.9,
        inspection_required=True,
        conflicts=[conflict],
        source_aware_conflicts=shape_source_aware_conflicts(
            [conflict], findings, value.uploaded_evidence
        ),
        current_evidence_findings=findings,
        requested_actions=[UploadDocumentRequestedAction(
            action_id="ACT-AUTO-RECONCILE",
            review_id="AUTONOMOUS-AUTO-RECONCILE",
            document_type="damage_evidence",
            instruction="Upload the correct rear damage and plate image.",
            replaces_document_id="DOC-BAD",
        )],
        requires_human_review=False,
        operational_indicators=OperationalIndicators(safety_concern=True),
    )


class ResumeReconciliationTests(unittest.TestCase):
    def test_current_identified_damage_evidence_binds_to_unique_outlier(self) -> None:
        evidence = metadata()
        reconciled = _reconcile_current_document_as_replacement(
            current_document=current_document(capabilities=(
                "damage_evidence", "license_plate_photo", "vehicle_identity"
            )),
            review_result=conflicted_review(evidence),
            metadata=evidence,
        )

        self.assertIsNotNone(reconciled)
        self.assertEqual(reconciled.replaces_document_id, "DOC-BAD")
        self.assertEqual(reconciled.requested_action_id, "ACT-AUTO-RECONCILE")

    def test_ambiguous_disagreement_without_authority_does_not_bind(self) -> None:
        evidence = metadata(include_report=False)
        self.assertIsNone(_reconcile_current_document_as_replacement(
            current_document=current_document(capabilities=(
                "damage_evidence", "license_plate_photo", "vehicle_identity"
            )),
            review_result=conflicted_review(evidence),
            metadata=evidence,
        ))

    def test_identity_only_upload_does_not_supersede_damage_evidence(self) -> None:
        evidence = metadata()
        self.assertIsNone(_reconcile_current_document_as_replacement(
            current_document=current_document(capabilities=(
                "license_plate_photo", "vehicle_identity"
            )),
            review_result=conflicted_review(evidence),
            metadata=evidence,
        ))

    def test_new_document_that_is_the_outlier_does_not_replace_another_image(self) -> None:
        evidence = metadata()
        review = conflicted_review(evidence)
        current_is_bad = current_document(capabilities=(
            "damage_evidence", "license_plate_photo", "vehicle_identity"
        )).model_copy(update={
            "document_id": "DOC-BAD",
            "filename": "bad-front.jpg",
            "evidence_findings": ["A different silver SUV has front damage."],
        })

        self.assertIsNone(_reconcile_current_document_as_replacement(
            current_document=current_is_bad,
            review_result=review,
            metadata=evidence,
        ))


if __name__ == "__main__":
    unittest.main()
