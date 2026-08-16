import unittest

from app.domain.claimant_action_display import build_claimant_action_display
from app.models.requested_action import (
    EnterTextRequestedAction,
    UploadDocumentRequestedAction,
)


def upload_action(*, target: str = "DOC-BAD") -> UploadDocumentRequestedAction:
    return UploadDocumentRequestedAction(
        action_id="ACT-SECRET",
        review_id="HRV-SECRET",
        document_type="damage_evidence",
        instruction="Upload the correct vehicle photo.",
        replaces_document_id=target,
    )


def assertion(
    filename: str,
    document_id: str,
    document_type: str,
    *,
    replaceable: bool,
) -> dict[str, object]:
    return {
        "filename": filename,
        "document_id": document_id,
        "document_type": document_type,
        "replaceable": replaceable,
    }


class ClaimantActionDisplayTests(unittest.TestCase):
    def test_missing_identity_has_safe_grounded_copy(self) -> None:
        display = build_claimant_action_display(
            {"missing_documents": [
                {"type": "vehicle_identity"},
                {"type": "license_plate_photo"},
            ]},
            [],
        )

        self.assertEqual(display.title, "Vehicle identity not verified")
        self.assertIn("readable license plate", display.explanation)

    def test_damage_mismatch_uses_report_grounding(self) -> None:
        display = build_claimant_action_display(
            {"source_aware_conflicts": [{
                "field": "damage_location",
                "selected_outlier_document_id": "DOC-BAD",
                "assertions": [
                    assertion("report.pdf", "DOC-REPORT", "police_report", replaceable=False),
                    assertion("front.jpg", "DOC-BAD", "damage_evidence", replaceable=True),
                ],
            }]},
            [upload_action()],
        )

        self.assertEqual(display.title, "Evidence doesn't match")
        self.assertIn("different damage locations", display.explanation)

    def test_combined_followup_mismatch_has_flow_4_copy_without_ids(self) -> None:
        display = build_claimant_action_display(
            {
                "source_aware_conflicts": [{
                    "field": "vehicle_identity_and_damage_location",
                    "selected_outlier_document_id": "DOC-BAD",
                    "assertions": [
                        assertion("IMG_5420.png", "DOC-BAD", "license_plate_photo", replaceable=True),
                        assertion("IMG_5419.png", "DOC-GOOD", "license_plate_photo", replaceable=True),
                    ],
                }],
                "current_evidence_findings": [{
                    "source": "police-report.pdf",
                    "finding": "Rear-end collision with rear damage.",
                }],
            },
            [upload_action()],
        )

        self.assertEqual(display.title, "New evidence doesn't match")
        self.assertIn("different vehicle", display.explanation)
        serialized = display.model_dump_json()
        self.assertNotIn("DOC-", serialized)
        self.assertNotIn("ACT-", serialized)
        self.assertNotIn("HRV-", serialized)

    def test_enter_text_reason_requires_grounded_matching_conflict(self) -> None:
        action = EnterTextRequestedAction(
            action_id="ACT-TEXT",
            review_id="HRV-TEXT",
            field_name="policy_number",
            instruction="Confirm the policy number.",
        )
        self.assertIsNone(build_claimant_action_display({}, [action]))

        display = build_claimant_action_display(
            {"conflicts": [{"field": "policy_number"}]}, [action]
        )
        self.assertEqual(display.title, "Policy information doesn't match")

    def test_no_grounded_upload_reason_is_omitted(self) -> None:
        self.assertIsNone(
            build_claimant_action_display({}, [upload_action(target="DOC-UNKNOWN")])
        )


if __name__ == "__main__":
    unittest.main()
