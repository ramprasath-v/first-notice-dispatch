import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.api.claimant import create_claimant_app
from app.models.claim_api import ClaimAcceptedResponse


class ClaimantPublicApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MagicMock()
        self.client = TestClient(
            create_claimant_app(
                self.service,
                ["http://localhost:4200", "https://firstnotice-ai.web.app"],
            )
        )

    def test_browser_api_exposes_claim_submission_under_api_prefix(self) -> None:
        self.service.submit_claim.return_value = ClaimAcceptedResponse(
            claim_id="CLM-A1B2C3D4",
            status="new",
            event_id="event-123",
            message="Claim received and processing started.",
        )

        response = self.client.post(
            "/api/claims",
            headers={"X-Idempotency-Key": "request-key-123"},
            data={"incident_description": "Rear-ended at a stoplight"},
            files=[("files", ("accident.jpg", b"image", "image/jpeg"))],
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["claim_id"], "CLM-A1B2C3D4")

    def test_browser_api_does_not_expose_pubsub_or_unprefixed_claims(self) -> None:
        self.assertEqual(self.client.post("/events/pubsub", json={}).status_code, 404)
        self.assertEqual(self.client.get("/claims/CLM-A1B2C3D4").status_code, 404)

    def test_browser_api_allows_only_configured_origin(self) -> None:
        allowed = self.client.options(
            "/api/claims",
            headers={
                "Origin": "https://firstnotice-ai.web.app",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Idempotency-Key",
            },
        )
        rejected = self.client.options(
            "/api/claims",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.headers["access-control-allow-origin"],
            "https://firstnotice-ai.web.app",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn("access-control-allow-origin", rejected.headers)


if __name__ == "__main__":
    unittest.main()
