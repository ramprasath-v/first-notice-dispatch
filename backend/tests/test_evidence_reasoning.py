import itertools
import unittest

from app.domain.evidence_reasoning import (
    canonical_active_evidence,
    select_corroborated_image_outlier,
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
    def test_partial_atomic_facts_preserve_safe_composite_vehicle_outlier(
        self,
    ) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type=document_type,
                filename=filename,
                document_id=document_id,
                source_identity=f"document:{document_id}",
                document_type=document_type,
                evidence_findings=findings,
            )
            for document_type, filename, document_id, findings in (
                (
                    "policy_document", "insurance2.pdf", "DOC-POLICY",
                    [
                        "vehicle_identity: 2014 Toyota Corolla (Dark Grey)",
                        "vehicle_make: Toyota", "vehicle_model: Corolla",
                        "vehicle_year: 2014", "license_plate: 7ABX123",
                    ],
                ),
                (
                    "police_report", "policeReport2.pdf", "DOC-REPORT",
                    [
                        "vehicle_identity: 2014 Toyota Corolla (Dark Grey)",
                        "vehicle_make: Toyota", "vehicle_model: Corolla",
                        "vehicle_year: 2014", "license_plate: 7ABX123",
                    ],
                ),
                (
                    "license_plate_photo", "image3.jpg", "DOC-WRONG",
                    ["vehicle_identity: Honda SUV", "vehicle_make: Honda"],
                ),
                (
                    "license_plate_photo", "imageL2.png", "DOC-CORRECT",
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

        assessment = shape_source_aware_conflicts(
            [conflict], [], uploaded, []
        )[0]

        self.assertEqual(
            assessment.selected_outlier_document_id, "DOC-WRONG"
        )
        self.assertEqual(
            {item.filename for item in assessment.assertions},
            {"insurance2.pdf", "policeReport2.pdf", "image3.jpg"},
        )

    def test_complete_atomic_vehicle_disagreement_remains_authoritative(
        self,
    ) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type=document_type,
                filename=filename,
                document_id=document_id,
                source_identity=f"document:{document_id}",
                document_type=document_type,
                evidence_findings=[
                    f"vehicle_identity: {identity}", f"vin: {vin}"
                ],
            )
            for document_type, filename, document_id, identity, vin in (
                (
                    "policy_document", "policy.pdf", "DOC-POLICY",
                    "Honda Accord", "VIN-SAME",
                ),
                (
                    "police_report", "report.pdf", "DOC-REPORT",
                    "Toyota Corolla", "VIN-SAME",
                ),
                (
                    "damage_evidence", "vehicle.png", "DOC-PHOTO",
                    "Toyota Corolla", "VIN-DIFFERENT",
                ),
            )
        ]
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["Honda Accord", "Toyota Corolla"],
            sources=["policy.pdf", "report.pdf", "vehicle.png"],
            reason="The submitted sources identify different vehicles.",
        )

        assessment = shape_source_aware_conflicts(
            [conflict], [], uploaded, []
        )[0]

        self.assertEqual({item.field for item in assessment.assertions}, {"vin"})
        self.assertEqual(
            assessment.selected_outlier_document_id, "DOC-PHOTO"
        )

    def test_incomplete_one_vs_one_vehicle_conflict_remains_ambiguous(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type="police_report",
                filename="report.pdf",
                document_id="DOC-REPORT",
                source_identity="document:DOC-REPORT",
                document_type="police_report",
                evidence_findings=["vehicle_identity: Toyota Corolla"],
            ),
            UploadedEvidence(
                evidence_type="license_plate_photo",
                filename="image.jpg",
                document_id="DOC-IMAGE",
                source_identity="document:DOC-IMAGE",
                document_type="license_plate_photo",
                evidence_findings=[
                    "vehicle_identity: Honda SUV", "vehicle_make: Honda"
                ],
            ),
        ]
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["Toyota Corolla", "Honda SUV"],
            sources=["report.pdf", "image.jpg"],
            reason="The two submitted sources identify different vehicles.",
        )

        assessment = shape_source_aware_conflicts(
            [conflict], [], uploaded, []
        )[0]

        self.assertIsNone(assessment.selected_outlier_document_id)

    def test_canonical_facts_reconstruct_three_source_two_value_conflict(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type="policy_document",
                filename="policy.pdf",
                document_id="DOC-POLICY",
                source_identity="document:DOC-POLICY",
                document_type="policy_document",
                evidence_findings=[
                    "The policy describes an unrelated vehicle.",
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
                evidence_findings=[
                    "vehicle_identity: Toyota Corolla",
                    "vehicle_make: Toyota",
                    "vehicle_model: Corolla",
                ],
            ),
        ]
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["2022 Honda Accord", "2014 Toyota Corolla"],
            sources=["policy.pdf", "report.pdf", "vehicle.png"],
            reason="The policy vehicle differs from the incident evidence.",
        )

        assessment = shape_source_aware_conflicts(
            [conflict], [], uploaded, []
        )[0]

        self.assertEqual(
            {item.filename: item.value for item in assessment.assertions},
            {
                "policy.pdf": "honda accord",
                "report.pdf": "toyota corolla",
                "vehicle.png": "toyota corolla",
            },
        )
        self.assertEqual(
            assessment.selected_outlier_document_id, "DOC-POLICY"
        )

    def test_exact_vin_match_corroborates_identity(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type=document_type,
                filename=filename,
                document_id=document_id,
                source_identity=f"document:{document_id}",
                document_type=document_type,
                evidence_findings=[f"vin: {vin}"],
            )
            for document_type, filename, document_id, vin in (
                ("policy_document", "policy.pdf", "DOC-POLICY", "VIN-ONE"),
                ("police_report", "report.pdf", "DOC-REPORT", "VIN-TWO"),
                ("damage_evidence", "vehicle.png", "DOC-PHOTO", "VIN-TWO"),
            )
        ]
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["vehicle one", "vehicle two"],
            sources=["policy.pdf", "report.pdf", "vehicle.png"],
            reason="The sources identify different vehicles.",
        )

        assessment = shape_source_aware_conflicts(
            [conflict], [], uploaded, []
        )[0]

        self.assertEqual(
            {item.field for item in assessment.assertions}, {"vin"}
        )
        self.assertEqual(
            assessment.selected_outlier_document_id, "DOC-POLICY"
        )

    def test_exact_plate_match_corroborates_identity(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type=document_type,
                filename=filename,
                document_id=document_id,
                source_identity=f"document:{document_id}",
                document_type=document_type,
                evidence_findings=[f"license_plate: {plate}"],
            )
            for document_type, filename, document_id, plate in (
                ("policy_document", "policy.pdf", "DOC-POLICY", "OLD123"),
                ("police_report", "report.pdf", "DOC-REPORT", "7ABX123"),
                ("damage_evidence", "vehicle.png", "DOC-PHOTO", "7ABX123"),
            )
        ]
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["vehicle one", "vehicle two"],
            sources=["policy.pdf", "report.pdf", "vehicle.png"],
            reason="The sources identify different vehicles.",
        )

        assessment = shape_source_aware_conflicts(
            [conflict], [], uploaded, []
        )[0]

        self.assertEqual(
            {item.field for item in assessment.assertions}, {"license_plate"}
        )
        self.assertEqual(
            assessment.selected_outlier_document_id, "DOC-POLICY"
        )

    def test_multiple_canonical_values_leave_source_unresolved(self) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type="policy_document",
                filename="policy.pdf",
                document_id="DOC-POLICY",
                source_identity="document:DOC-POLICY",
                document_type="policy_document",
                evidence_findings=[
                    "vehicle_identity: Honda Accord",
                    "vehicle_identity: Nissan Altima",
                ],
            ),
            UploadedEvidence(
                evidence_type="police_report",
                filename="report.pdf",
                document_id="DOC-REPORT",
                source_identity="document:DOC-REPORT",
                document_type="police_report",
                evidence_findings=["vehicle_identity: Toyota Corolla"],
            ),
            UploadedEvidence(
                evidence_type="damage_evidence",
                filename="vehicle.png",
                document_id="DOC-PHOTO",
                source_identity="document:DOC-PHOTO",
                document_type="damage_evidence",
                evidence_findings=["vehicle_identity: Toyota Corolla"],
            ),
        ]
        conflict = EvidenceConflict(
            field="vehicle_identity",
            values=["Honda Accord", "Toyota Corolla"],
            sources=["policy.pdf", "report.pdf", "vehicle.png"],
            reason="The sources disagree.",
        )

        assessment = shape_source_aware_conflicts(
            [conflict], [], uploaded, []
        )[0]

        self.assertNotIn(
            "policy.pdf", {item.filename for item in assessment.assertions}
        )
        self.assertIsNone(assessment.selected_outlier_document_id)

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
        persisted = [
            item.model_copy(update={"status": "superseded"})
            if item.document_id == "DOC-FRONT"
            else item
            for item in evidence()
        ]
        snapshot = canonical_active_evidence(persisted)

        self.assertNotIn("DOC-FRONT", {item.document_id for item in snapshot})
        self.assertNotIn(
            "vehicle_identity",
            {capability for item in snapshot for capability in item.supported_capabilities},
        )

    def test_live_flow_4_shape_selects_bad_followup_across_order_and_issue_shapes(
        self,
    ) -> None:
        uploaded = [
            UploadedEvidence(
                evidence_type="police_report", filename="police-report.pdf",
                document_id="DOC-REPORT", source_identity="document:DOC-REPORT",
                document_type="police_report", usable=True,
                evidence_findings=["Rear-end collision with rear damage."],
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
                    "A material safety concern is visible.",
                ],
            ),
            UploadedEvidence(
                evidence_type="damage_evidence", filename="IMG_5419.png",
                document_id="DOC-CORRECT", source_identity="document:DOC-CORRECT",
                document_type="license_plate_photo", usable=True,
                evidence_findings=["A grey sedan has rear damage and a readable plate."],
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
        findings = [
            CurrentEvidenceFinding(source=item.filename, finding=finding)
            for item in uploaded
            for finding in item.evidence_findings
        ]
        conflict_shapes = [
            EvidenceConflict(
                field="vehicle_identity",
                values=["different vehicles", "identified vehicle"],
                sources=["IMG_5420.png", "IMG_5419.png"],
                reason="The images show different vehicles.",
            ),
            EvidenceConflict(
                field="vehicle_identity_and_damage_location",
                values=["silver/front", "grey/rear"],
                sources=["IMG_5420.png", "IMG_5419.png"],
                reason="Vehicle and damage evidence disagree.",
            ),
            EvidenceConflict(
                field="DAMAGE-LOCATION-AND-VEHICLE-IDENTITY",
                values=["silver/front", "grey/rear"],
                sources=["img_5420.PNG", "img_5419.PNG"],
                reason="Equivalent conflict with alternate casing and ordering.",
            ),
        ]
        uncertainty = UnresolvedUncertainty(
            uncertainty=(
                "The front-damage vehicle may not be the same vehicle as the "
                "rear-damage vehicle."
            ),
            sources=["img_5419.PNG", "IMG_5420.png"],
        )

        results = []
        for ordered in (uploaded, list(reversed(uploaded))):
            for conflict in conflict_shapes:
                results.append(select_corroborated_image_outlier(
                    [conflict], [], list(reversed(findings)), ordered
                ))
            results.append(select_corroborated_image_outlier(
                [], [uncertainty], findings, ordered
            ))

        self.assertTrue(results)
        self.assertEqual({item.document_id for item in results if item}, {"DOC-BAD"})
        self.assertTrue(all(item is not None for item in results))


if __name__ == "__main__":
    unittest.main()
