import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.api.claimant import create_claimant_app
from app.events.claim_events import (
    ClaimHumanReviewApprovedEvent,
    ClaimHumanReviewCorrectionRequestedEvent,
)
from app.integrations.gmail_service import GmailError, GmailSendResult
from app.models.human_review import (
    HumanReviewBriefing,
    HumanReviewDecisionRequest,
    HumanReviewDecisionResponse,
    HumanReviewPublicResponse,
    HumanReviewRecord,
)
from app.services.human_review_service import (
    HumanReviewExpiredError,
    HumanReviewResumeWorkflow,
    HumanReviewService,
    HumanReviewSettings,
    hash_review_token,
)


NOW = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
CLAIM_ID = "CLM-A1B2C3D4"
REVIEW_ID = "HRV-123456789ABC"


def briefing() -> HumanReviewBriefing:
    return HumanReviewBriefing(
        reason="A significant factual conflict requires human review.",
        summary="A policy-number conflict paused routing.",
        known_facts=["Claim type: auto_collision"],
        conflicts=["policy_number: POL-1001 versus POL-9999"],
        unresolved_questions=["Verify the correct value for policy_number."],
        recommended_next_action="Verify the correct value for policy_number.",
        confidence=0.91,
    )


def record(**updates) -> HumanReviewRecord:
    values = dict(
        review_id=REVIEW_ID,
        claim_id=CLAIM_ID,
        status="pending",
        reason="A significant factual conflict requires human review.",
        briefing=briefing(),
        conflict_fields=["policy_number"],
        token_hash=hash_review_token("secure-token"),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        correlation_id="corr-review",
    )
    values.update(updates)
    return HumanReviewRecord(**values)


class HumanReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "claim_type": "auto_collision",
            "incident_summary": "Rear impact.",
            "human_review_reason": "A significant factual conflict requires human review.",
            "review_confidence": 0.91,
            "conflicts": [
                {
                    "field": "policy_number",
                    "values": ["POL-1001", "POL-9999"],
                    "sources": ["claimant", "police-report.pdf"],
                    "reason": "Values differ.",
                }
            ],
        }
        self.repository.get_human_review.return_value = None
        self.repository.create_human_review.return_value = True
        self.publisher = MagicMock()
        self.publisher.publish.return_value = "message-123"
        self.gmail = MagicMock()
        self.gmail.send_human_review_email.return_value = GmailSendResult(
            gmail_message_id="gmail-123",
            sent_at=NOW,
            recipient="firstnotice.adjuster@gmail.com",
            sender="sender@gmail.com",
        )
        self.service = HumanReviewService(
            repository=self.repository,
            publisher=self.publisher,
            settings=HumanReviewSettings("https://firstnotice-web.example", 60),
            gmail_sender=self.gmail,
            recipient="firstnotice.adjuster@gmail.com",
            sender="sender@gmail.com",
        )

    def test_human_review_required_creates_one_secure_review_and_email(self) -> None:
        review = self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        created = self.repository.create_human_review.call_args.args[0]
        request = self.gmail.send_human_review_email.call_args.args[0]
        plaintext_token = request.review_url.rsplit("/", 1)[1]
        self.assertEqual(hash_review_token(plaintext_token), created.token_hash)
        self.assertNotIn(plaintext_token, created.model_dump().values())
        self.assertIn("/adjuster/review/", request.review_url)
        self.assertIn(CLAIM_ID, request.subject)
        self.assertEqual(review.notification_status, "sent")

    def test_duplicate_checkpoint_reuses_sent_review_without_email(self) -> None:
        self.repository.get_human_review.return_value = record(notification_status="sent")

        first = self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")
        second = self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        self.assertEqual(first.review_id, second.review_id)
        self.repository.create_human_review.assert_not_called()
        self.gmail.send_human_review_email.assert_not_called()

    def test_review_email_failure_does_not_continue_claim(self) -> None:
        self.gmail.send_human_review_email.side_effect = GmailError(
            "temporary", retryable=True
        )

        with self.assertRaises(GmailError):
            self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        self.publisher.publish.assert_not_called()
        created_review = self.repository.create_human_review.call_args.args[0]
        self.repository.mark_human_review_notification.assert_called_with(
            CLAIM_ID,
            created_review.review_id,
            status="failed",
            correlation_id="corr-review",
        )

    def test_invalid_and_expired_tokens_are_rejected(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = None
        with self.assertRaisesRegex(Exception, "invalid"):
            self.service.get_public_review("invalid-token")

        self.repository.get_human_review_by_token_hash.return_value = record(
            expires_at=NOW - timedelta(minutes=1)
        )
        with patch("app.services.human_review_service.datetime") as clock:
            clock.now.return_value = NOW
            clock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            with self.assertRaises(HumanReviewExpiredError):
                self.service.get_public_review("expired-token")

    def test_approve_consumes_token_and_publishes_versioned_event(self) -> None:
        approved = record(
            status="approved",
            decision_at=NOW,
            decision_event_id=f"{CLAIM_ID}:{REVIEW_ID}:approved:v1",
            decision_publish_status="pending",
        )
        self.repository.decide_human_review.return_value = (approved, False)

        result = self.service.approve("secure-token", HumanReviewDecisionRequest())

        event = self.publisher.publish.call_args.args[0]
        self.assertIsInstance(event, ClaimHumanReviewApprovedEvent)
        self.assertEqual(event.payload.review_id, REVIEW_ID)
        self.assertNotIn("secure-token", event.model_dump_json())
        self.assertEqual(result.status, "approved")

    def test_duplicate_approve_is_idempotent_and_does_not_republish(self) -> None:
        approved = record(
            status="approved",
            decision_at=NOW,
            decision_event_id=f"{CLAIM_ID}:{REVIEW_ID}:approved:v1",
            decision_publish_status="published",
        )
        self.repository.decide_human_review.return_value = (approved, True)

        result = self.service.approve("secure-token", HumanReviewDecisionRequest())

        self.assertTrue(result.duplicate)
        self.publisher.publish.assert_not_called()

    def test_correction_decision_publishes_distinct_event(self) -> None:
        correction = record(
            status="correction_requested",
            decision_at=NOW,
            decision_event_id=f"{CLAIM_ID}:{REVIEW_ID}:correction_requested:v1",
            decision_publish_status="pending",
        )
        self.repository.decide_human_review.return_value = (correction, False)

        self.service.request_correction(
            "secure-token", HumanReviewDecisionRequest(decision_note="Confirm policy")
        )

        self.assertIsInstance(
            self.publisher.publish.call_args.args[0],
            ClaimHumanReviewCorrectionRequestedEvent,
        )


class HumanReviewResumeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.workflow = HumanReviewResumeWorkflow(self.repository)

    def test_approval_resumes_same_claim_to_inspection_when_complete(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "conflicts": [{"field": "policy_number"}],
            "missing_documents": [],
            "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(status="approved")

        result = self.workflow.resume_approved(CLAIM_ID, REVIEW_ID, "corr")

        self.assertEqual(result["final_status"], "inspection_pending")
        call = self.repository.complete_human_review_resume.call_args.kwargs
        self.assertEqual(call["claim_id"], CLAIM_ID)
        self.assertEqual(call["conflicts"], [])

    def test_approval_does_not_bypass_unresolved_missing_requirements(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "conflicts": [{"field": "policy_number"}],
            "missing_documents": [{"type": "police_report"}],
            "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(status="approved")

        result = self.workflow.resume_approved(CLAIM_ID, REVIEW_ID, "corr")

        self.assertEqual(result["final_status"], "awaiting_documents")

    def test_correction_request_reuses_claimant_requested_action(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "conflicts": [{"field": "policy_number"}],
            "missing_documents": [],
            "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(
            status="correction_requested"
        )

        result = self.workflow.request_correction(CLAIM_ID, REVIEW_ID, "corr")

        self.assertEqual(result["final_status"], "awaiting_documents")
        action = self.repository.complete_human_review_resume.call_args.kwargs[
            "requested_actions"
        ][0]
        self.assertEqual(action["action_type"], "enter_text")
        self.assertEqual(action["field_name"], "policy_number")


class HumanReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.claim_service = MagicMock()
        self.review_service = MagicMock()
        self.client = TestClient(
            create_claimant_app(
                self.claim_service,
                ["http://localhost:4200"],
                self.review_service,
            )
        )

    def test_review_api_exposes_only_token_scoped_operations(self) -> None:
        self.review_service.get_public_review.return_value = HumanReviewPublicResponse(
            review_id=REVIEW_ID,
            claim_id=CLAIM_ID,
            status="pending",
            reason=briefing().reason,
            briefing=briefing(),
            expires_at=NOW + timedelta(hours=1),
        )
        self.review_service.approve.return_value = HumanReviewDecisionResponse(
            review_id=REVIEW_ID,
            claim_id=CLAIM_ID,
            status="approved",
            event_id="approve-event",
            message="Review approved.",
        )

        headers = {"X-Review-Token": "secure-review-token-123"}
        self.assertEqual(
            self.client.get("/api/reviews/current", headers=headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/api/reviews/current/approve", json={}, headers=headers
            ).status_code,
            202,
        )
        self.assertEqual(self.client.patch(f"/api/claims/{CLAIM_ID}", json={}).status_code, 405)
        self.assertEqual(self.client.post("/events/pubsub", json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
