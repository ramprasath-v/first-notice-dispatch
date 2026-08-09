import itertools
import unittest

from app.domain.evidence_reasoning import (
    canonical_active_evidence,
    shape_source_aware_conflicts,
    shape_source_aware_uncertainties,
)
from app.models.review_result import (
    CurrentEvidenceFinding,
    EvidenceConflict,
    UnresolvedUncertainty,
    UploadedEvidence,
)


def evidence() -> list[UploadedEvidence]:
    return [
        UploadedEvidence(
            evidence_type="police_report",
            filename="police-report.pdf",
            document_id="DOC-REPORT",
            source_identity="document:DOC-REPORT",
            document_type="police_report",
        ),
        UploadedEvidence(
            evidence_type="damage_evidence",
            filename="original-rear.jpg",
            document_id="DOC-REAR",
            source_identity="document:DOC-REAR",
            document_type="damage_evidence",
            usable=True,
        ),
        UploadedEvidence(
            evidence_type="license_plate_photo",
            filename="followup-front.jpg",
            document_id="DOC-FRONT",
            source_identity="document:DOC-FRONT",
            document_type="license_plate_photo",
            usable=True,
        ),
        UploadedEvidence(
            evidence_type="vehicle_identity",
            filename="followup-front.jpg",
            document_id="DOC-FRONT",
            source_identity="document:DOC-FRONT",
            document_type="license_plate_photo",
            usable=True,
        ),
        UploadedEvidence(
            evidence_type="damage_evidence",
            filename="followup-front.jpg",
            document_id="DOC-FRONT",
            source_identity="document:DOC-FRONT",
            document_type="license_plate_photo",
            usable=True,
        ),
    ]


FINDINGS = [
    CurrentEvidenceFinding(
        source="police-report.pdf", finding="The report describes rear-end damage."
    ),
    CurrentEvidenceFinding(
        source="original-rear.jpg", finding="Rear damage is visible."
    ),
    CurrentEvidenceFinding(
        source="followup-front.jpg",
        finding="The plate is readable and front-end damage is visible.",
    ),
]

CONFLICT = EvidenceConflict(
    field="damage_location",
    values=["rear", "front"],
    sources=["police-report.pdf", "original-rear.jpg", "followup-front.jpg"],
    reason="The submitted evidence depicts conflicting damage locations.",
)


class EvidenceReasoningTests(unittest.TestCase):
    def test_flow_b_selects_multi_capability_front_photo_as_safe_outlier(self) -> None:
        assessment = shape_source_aware_conflicts(
            [CONFLICT], FINDINGS, evidence()
        )[0]

        self.assertEqual(assessment.selected_outlier_document_id, "DOC-FRONT")
        front = next(
            item for item in canonical_active_evidence(evidence())
            if item.document_id == "DOC-FRONT"
        )
        self.assertEqual(
            set(front.supported_capabilities),
            {"damage_evidence", "license_plate_photo", "vehicle_identity"},
        )

    def test_conflict_fingerprint_and_target_are_invariant_to_input_order(self) -> None:
        expected: tuple[str, str | None] | None = None
        for values in itertools.permutations(evidence()):
            assessment = shape_source_aware_conflicts(
                [CONFLICT], list(reversed(FINDINGS)), list(values)
            )[0]
            current = (
                assessment.fingerprint,
                assessment.selected_outlier_document_id,
            )
            expected = expected or current
            self.assertEqual(current, expected)

    def test_two_photos_without_independent_corroboration_remain_ambiguous(self) -> None:
        ambiguous_evidence = [
            item for item in evidence()
            if item.document_id in {"DOC-REAR", "DOC-FRONT"}
        ]
        conflict = CONFLICT.model_copy(
            update={"sources": ["original-rear.jpg", "followup-front.jpg"]}
        )

        assessment = shape_source_aware_conflicts(
            [conflict], FINDINGS, ambiguous_evidence
        )[0]

        self.assertIsNone(assessment.selected_outlier_document_id)

    def test_source_complete_uncertainty_uses_report_corroboration(self) -> None:
        uncertainty = UnresolvedUncertainty(
            uncertainty=(
                "The relationship of the front-damage vehicle in followup-front.jpg "
                "to the rear-end collision vehicle in original-rear.jpg is unclear."
            ),
            sources=["original-rear.jpg", "followup-front.jpg"],
            source_attribution_incomplete=False,
            fingerprint="CFP-FLOW-4",
        )

        assessment = shape_source_aware_uncertainties(
            [uncertainty], FINDINGS, evidence()
        )[0]

        self.assertEqual(assessment.category, "damage_location")
        self.assertEqual(assessment.selected_outlier_document_id, "DOC-FRONT")

    def test_uncertainty_target_is_invariant_to_evidence_order(self) -> None:
        uncertainty = UnresolvedUncertainty(
            uncertainty="Front versus rear damage relationship is unclear.",
            sources=["followup-front.jpg", "original-rear.jpg"],
            fingerprint="CFP-FLOW-4",
        )
        expected = None
        for values in itertools.permutations(evidence()):
            selected = shape_source_aware_uncertainties(
                [uncertainty], list(reversed(FINDINGS)), list(values)
            )[0].selected_outlier_document_id
            expected = expected or selected
            self.assertEqual(selected, expected)

    def test_uncertainty_without_police_corroboration_does_not_guess(self) -> None:
        uncertainty = UnresolvedUncertainty(
            uncertainty="Front versus rear damage relationship is unclear.",
            sources=["followup-front.jpg", "original-rear.jpg"],
            fingerprint="CFP-AMBIGUOUS",
        )
        photos_only = [
            item for item in evidence() if item.document_id != "DOC-REPORT"
        ]
        photo_findings = [
            item for item in FINDINGS if item.source != "police-report.pdf"
        ]

        assessment = shape_source_aware_uncertainties(
            [uncertainty], photo_findings, photos_only
        )[0]

        self.assertIsNone(assessment.selected_outlier_document_id)

    def test_superseded_evidence_is_absent_from_canonical_input(self) -> None:
        active = [item for item in evidence() if item.document_id != "DOC-FRONT"]
        snapshot = canonical_active_evidence(active)

        self.assertNotIn("DOC-FRONT", {item.document_id for item in snapshot})
        self.assertNotIn(
            "vehicle_identity",
            {capability for item in snapshot for capability in item.supported_capabilities},
        )


if __name__ == "__main__":
    unittest.main()
