import unittest

from app.domain.claimant_evidence_requests import build_claimant_evidence_requests


def missing(requirement: str, reason: str | None = None) -> dict[str, str]:
    return {
        "type": requirement,
        "reason": reason or f"Missing {requirement}.",
        "source_requirement": requirement,
    }


class ClaimantEvidenceRequestTests(unittest.TestCase):
    def test_identity_and_plate_requirements_become_one_physical_request(self) -> None:
        requests = build_claimant_evidence_requests(
            [missing("vehicle_identity"), missing("license_plate_photo")]
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].document_type, "license_plate_photo")
        self.assertEqual(requests[0].label, "License Plate Photo")
        self.assertEqual(
            requests[0].instruction,
            "Please upload a clear photo of your vehicle's license plate.",
        )
        self.assertEqual(
            set(requests[0].satisfies_requirements),
            {"vehicle_identity", "license_plate_photo"},
        )

    def test_unreadable_plate_becomes_one_replacement_request(self) -> None:
        requests = build_claimant_evidence_requests(
            [],
            [
                {
                    "evidence_type": "license_plate_photo",
                    "reason": "The plate is too blurry to read.",
                }
            ],
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].document_type, "license_plate_photo")
        self.assertTrue(requests[0].replacement_required)
        self.assertEqual(
            set(requests[0].satisfies_requirements),
            {"vehicle_identity", "license_plate_photo"},
        )

    def test_different_physical_artifacts_remain_separate_requests(self) -> None:
        requests = build_claimant_evidence_requests(
            [missing("police_report"), missing("license_plate_photo")]
        )

        self.assertEqual(len(requests), 2)
        self.assertEqual(
            {request.document_type for request in requests},
            {"police_report", "license_plate_photo"},
        )

    def test_incident_date_is_never_projected_as_uploadable_evidence(self) -> None:
        requests = build_claimant_evidence_requests([missing("incident_date")])

        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
