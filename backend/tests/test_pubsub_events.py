import base64
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from google.api_core.exceptions import AlreadyExists
from pydantic import ValidationError

from app.agents.firstnotice_adk import build_firstnotice_coordinator
from app.api.pubsub import (
    PubSubPushEnvelope,
    create_app,
    decode_push_envelope,
)
from app.events.claim_event_handler import (
    ClaimEventHandler,
    ClaimEventProcessingError,
    EventHandlingResult,
)
from app.integrations.gmail_service import GmailError
from app.services.claim_review_service import ClaimReviewError
from app.events.claim_events import (
    CLAIM_EVENT_ADAPTER,
    ClaimCorrectionReceivedEvent,
    ClaimDocumentReceivedEvent,
    ClaimHumanReviewApprovedEvent,
    ClaimInspectionReadyEvent,
    ClaimSubmittedEvent,
    DocumentReceivedPayload,
    CorrectionReceivedPayload,
    HumanReviewPayload,
    inspection_ready_event_id,
    parse_claim_event_json,
)
from app.events.coordinator_invoker import AdkClaimCoordinatorInvoker
from app.events.pubsub_publisher import ClaimEventPublisher, PubSubSettings
from app.models.adk_orchestration import ClaimStateResult
from app.tools.firestore_repository import FirestoreClaimRepository
from app.workflows.claim_dispatch_workflow import ClaimDispatchWorkflowError


CLAIM_ID = "CLM-A1B2C3D4"
EVENT_ID = "event-123"


def submitted_event() -> ClaimSubmittedEvent:
    return ClaimSubmittedEvent(
        event_id=EVENT_ID,
        event_type="claim.submitted",
        claim_id=CLAIM_ID,
        correlation_id="corr-123",
    )


def push_body(event: ClaimSubmittedEvent) -> dict[str, object]:
    encoded = base64.b64encode(event.model_dump_json().encode()).decode()
    return {"message": {"data": encoded, "messageId": "message-123"}}


class ClaimEventHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.repository.begin_claim_event.return_value = True
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
        }
        self.coordinator = MagicMock()
        self.coordinator.process_submitted_claim = AsyncMock(
            return_value={"kind": "review_completed"}
        )
        self.resume = MagicMock()
        self.dispatch = MagicMock()
        self.publisher = MagicMock()
        self.publisher.publish.return_value = "inspection-ready-message"
        self.human_review_service = MagicMock()
        self.human_review_service.ensure_review_requested.return_value = MagicMock(
            review_id="HRV-123", notification_status="sent"
        )
        self.human_resume = MagicMock()
        self.handler = ClaimEventHandler(
            repository=self.repository,
            coordinator=self.coordinator,
            resume_workflow=self.resume,
            dispatch_workflow=self.dispatch,
            publisher=self.publisher,
            human_review_service=self.human_review_service,
            human_review_resume_workflow=self.human_resume,
        )

    async def test_claim_submitted_routes_to_adk_coordinator(self) -> None:
        result = await self.handler.handle(submitted_event())

        self.coordinator.process_submitted_claim.assert_awaited_once_with(CLAIM_ID)
        self.repository.complete_claim_event.assert_called_once()
        self.assertEqual(result.outcome, "processed")

    async def test_document_received_routes_to_resume_workflow(self) -> None:
        document = MagicMock(document_id="DOC-5678")
        self.repository.get_document.return_value = document
        resume_result = MagicMock()
        resume_result.model_dump.return_value = {"final_status": "inspection_pending"}
        self.resume.resume.return_value = resume_result
        event = ClaimDocumentReceivedEvent(
            event_id=EVENT_ID,
            event_type="claim.document.received",
            claim_id=CLAIM_ID,
            payload=DocumentReceivedPayload(document_id="DOC-5678"),
        )

        await self.handler.handle(event)

        self.resume.resume.assert_called_once_with(CLAIM_ID, document)

    async def test_resource_exhausted_document_review_remains_retryable(self) -> None:
        document = MagicMock(document_id="DOC-5678")
        self.repository.get_document.return_value = document
        self.resume.resume.side_effect = ClaimReviewError(
            "Gemini evidence review failed: 429 RESOURCE_EXHAUSTED"
        )
        event = ClaimDocumentReceivedEvent(
            event_id=EVENT_ID,
            event_type="claim.document.received",
            claim_id=CLAIM_ID,
            payload=DocumentReceivedPayload(document_id="DOC-5678"),
        )

        with self.assertRaises(ClaimEventProcessingError) as raised:
            await self.handler.handle(event)

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.stage, "business_event_route")
        self.repository.complete_claim_event.assert_not_called()

    async def test_submitted_claim_publishes_inspection_ready_after_transition(self) -> None:
        self.repository.get_claim.side_effect = [
            {"claim_id": CLAIM_ID, "status": "intake_complete"},
            {"claim_id": CLAIM_ID, "status": "inspection_pending"},
        ]

        await self.handler.handle(submitted_event())

        ready = self.publisher.publish.call_args.args[0]
        self.assertIsInstance(ready, ClaimInspectionReadyEvent)
        self.assertEqual(ready.event_id, inspection_ready_event_id(CLAIM_ID))
        self.assertEqual(ready.correlation_id, "corr-123")

    async def test_resumed_claim_publishes_inspection_ready_after_transition(self) -> None:
        document = MagicMock(document_id="DOC-5678")
        self.repository.get_document.return_value = document
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "inspection_pending",
        }
        resume_result = MagicMock()
        resume_result.model_dump.return_value = {
            "final_status": "inspection_pending"
        }
        self.resume.resume.return_value = resume_result
        event = ClaimDocumentReceivedEvent(
            event_id=EVENT_ID,
            event_type="claim.document.received",
            claim_id=CLAIM_ID,
            payload=DocumentReceivedPayload(document_id="DOC-5678"),
        )

        await self.handler.handle(event)

        ready = self.publisher.publish.call_args.args[0]
        self.assertEqual(ready.event_type, "claim.inspection.ready")
        self.assertEqual(ready.event_id, inspection_ready_event_id(CLAIM_ID))

    async def test_awaiting_documents_does_not_publish_inspection_ready(self) -> None:
        await self.handler.handle(submitted_event())

        self.publisher.publish.assert_not_called()
        self.human_review_service.ensure_review_requested.assert_not_called()

    async def test_human_review_does_not_publish_inspection_ready(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
        }

        await self.handler.handle(submitted_event())

        self.publisher.publish.assert_not_called()
        self.human_review_service.ensure_review_requested.assert_not_called()

    async def test_inspection_ready_requests_decision_without_dispatch(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "inspection_ready",
        }

        await self.handler.handle(submitted_event())

        self.human_review_service.ensure_review_requested.assert_called_once_with(
            CLAIM_ID, correlation_id="corr-123"
        )
        self.publisher.publish.assert_not_called()

    async def test_decision_notification_failure_does_not_hide_durable_ready_state(self) -> None:
        self.repository.get_claim.side_effect = [
            {"claim_id": CLAIM_ID, "status": "review_processing"},
            {"claim_id": CLAIM_ID, "status": "inspection_ready"},
        ]
        self.human_review_service.ensure_review_requested.side_effect = GmailError(
            "Gmail temporarily unavailable", retryable=True
        )

        with self.assertRaises(ClaimEventProcessingError) as raised:
            await self.handler.handle(submitted_event())

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.stage, "inspection_decision_boundary")
        self.coordinator.process_submitted_claim.assert_awaited_once_with(CLAIM_ID)
        self.repository.fail_claim_event.assert_called_once()
        self.publisher.publish.assert_not_called()

    async def test_approved_review_resumes_same_claim_and_publishes_inspection(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "inspection_pending",
        }
        self.human_resume.resume_approved.return_value = {
            "final_status": "inspection_pending"
        }
        event = ClaimHumanReviewApprovedEvent(
            event_id="approve-event",
            event_type="claim.human_review.approved",
            claim_id=CLAIM_ID,
            correlation_id="corr-review",
            payload=HumanReviewPayload(review_id="HRV-123"),
        )

        await self.handler.handle(event)

        self.human_resume.resume_approved.assert_called_once_with(
            CLAIM_ID, "HRV-123", "corr-review"
        )
        ready = self.publisher.publish.call_args.args[0]
        self.assertEqual(ready.event_type, "claim.inspection.ready")
        self.assertEqual(ready.claim_id, CLAIM_ID)
        self.assertEqual(ready.event_id, inspection_ready_event_id(CLAIM_ID))

    async def test_approved_review_with_unresolved_evidence_does_not_publish_inspection(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
        }
        self.human_resume.resume_approved.return_value = {
            "final_status": "awaiting_documents"
        }
        event = ClaimHumanReviewApprovedEvent(
            event_id="approve-event",
            event_type="claim.human_review.approved",
            claim_id=CLAIM_ID,
            correlation_id="corr-review",
            payload=HumanReviewPayload(review_id="HRV-123"),
        )

        await self.handler.handle(event)

        self.publisher.publish.assert_not_called()

    async def test_duplicate_approval_reconciles_missing_inspection_ready_publication(self) -> None:
        self.repository.begin_claim_event.return_value = False
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "inspection_pending",
        }
        event = ClaimHumanReviewApprovedEvent(
            event_id="approve-event",
            event_type="claim.human_review.approved",
            claim_id=CLAIM_ID,
            correlation_id="corr-review",
            payload=HumanReviewPayload(review_id="HRV-123"),
        )

        result = await self.handler.handle(event)

        self.assertTrue(result.duplicate)
        self.human_resume.resume_approved.assert_not_called()
        ready = self.publisher.publish.call_args.args[0]
        self.assertEqual(ready.event_id, inspection_ready_event_id(CLAIM_ID))

    async def test_duplicate_event_reconciles_missing_human_review_checkpoint(self) -> None:
        self.repository.begin_claim_event.return_value = False
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "inspection_ready",
            "current_human_review_generation": 2,
            "current_human_review_id": "HRV-CYCLE-2",
        }

        result = await self.handler.handle(submitted_event())

        self.assertTrue(result.duplicate)
        self.coordinator.process_submitted_claim.assert_not_awaited()
        self.resume.resume.assert_not_called()
        self.human_review_service.ensure_review_requested.assert_called_once_with(
            CLAIM_ID, correlation_id="corr-123"
        )

    async def test_correction_resume_uses_same_inspection_dispatch_boundary(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "inspection_pending",
        }
        self.human_resume.resume_correction.return_value = {
            "final_status": "inspection_pending"
        }
        event = ClaimCorrectionReceivedEvent(
            event_id="correction-event",
            event_type="claim.correction.received",
            claim_id=CLAIM_ID,
            correlation_id="corr-correction",
            payload=CorrectionReceivedPayload(
                review_id="HRV-123", field_name="policy_number"
            ),
        )

        await self.handler.handle(event)

        self.human_resume.resume_correction.assert_called_once_with(
            CLAIM_ID, "HRV-123", "policy_number", "corr-correction"
        )
        ready = self.publisher.publish.call_args.args[0]
        self.assertEqual(ready.event_id, inspection_ready_event_id(CLAIM_ID))

    async def test_inspection_ready_routes_to_dispatch_workflow(self) -> None:
        dispatch_result = MagicMock()
        dispatch_result.model_dump.return_value = {"final_status": "adjuster_notified"}
        self.dispatch.dispatch.return_value = dispatch_result
        event = ClaimInspectionReadyEvent(
            event_id=EVENT_ID,
            event_type="claim.inspection.ready",
            claim_id=CLAIM_ID,
        )

        await self.handler.handle(event)

        self.dispatch.dispatch.assert_called_once_with(CLAIM_ID)
        self.publisher.publish.assert_not_called()

    async def test_duplicate_inspection_ready_event_dispatches_once(self) -> None:
        self.repository.begin_claim_event.side_effect = [True, False]
        dispatch_result = MagicMock()
        dispatch_result.model_dump.return_value = {
            "final_status": "adjuster_notified"
        }
        self.dispatch.dispatch.return_value = dispatch_result
        event = ClaimInspectionReadyEvent(
            event_id=inspection_ready_event_id(CLAIM_ID),
            event_type="claim.inspection.ready",
            claim_id=CLAIM_ID,
        )

        first = await self.handler.handle(event)
        second = await self.handler.handle(event)

        self.assertEqual(first.outcome, "processed")
        self.assertTrue(second.duplicate)
        self.dispatch.dispatch.assert_called_once_with(CLAIM_ID)

    async def test_duplicate_event_is_successful_no_op(self) -> None:
        self.repository.begin_claim_event.return_value = False

        result = await self.handler.handle(submitted_event())

        self.assertTrue(result.duplicate)
        self.coordinator.process_submitted_claim.assert_not_awaited()
        self.repository.complete_claim_event.assert_not_called()
        self.publisher.publish.assert_not_called()

    async def test_failure_is_recorded_without_marking_event_complete(self) -> None:
        self.dispatch.dispatch.side_effect = RuntimeError("temporary model outage")
        event = ClaimInspectionReadyEvent(
            event_id=EVENT_ID,
            event_type="claim.inspection.ready",
            claim_id=CLAIM_ID,
        )

        with self.assertRaises(ClaimEventProcessingError) as raised:
            await self.handler.handle(event)

        self.assertTrue(raised.exception.retryable)
        self.repository.complete_claim_event.assert_not_called()
        failure = self.repository.fail_claim_event.call_args.kwargs
        self.assertEqual(failure["error_type"], "RuntimeError")
        self.assertTrue(failure["retryable"])

    async def test_gmail_failure_preserves_retryability(self) -> None:
        self.dispatch.dispatch.side_effect = GmailError(
            "Gmail temporarily unavailable", retryable=True
        )
        event = ClaimInspectionReadyEvent(
            event_id=EVENT_ID,
            event_type="claim.inspection.ready",
            claim_id=CLAIM_ID,
        )

        with self.assertRaises(ClaimEventProcessingError) as raised:
            await self.handler.handle(event)

        self.assertTrue(raised.exception.retryable)
        failure = self.repository.fail_claim_event.call_args.kwargs
        self.assertEqual(failure["error_type"], "GmailError")
        self.assertTrue(failure["retryable"])

    async def test_missing_document_is_non_retryable(self) -> None:
        self.repository.get_document.return_value = None
        event = ClaimDocumentReceivedEvent(
            event_id=EVENT_ID,
            event_type="claim.document.received",
            claim_id=CLAIM_ID,
            payload=DocumentReceivedPayload(document_id="DOC-MISSING"),
        )

        with self.assertRaises(ClaimEventProcessingError) as raised:
            await self.handler.handle(event)

        self.assertFalse(raised.exception.retryable)
        self.resume.resume.assert_not_called()

    async def test_inspection_event_cannot_bypass_dispatch_state_rules(self) -> None:
        self.dispatch.dispatch.side_effect = ClaimDispatchWorkflowError(
            "cannot dispatch from status 'human_review_required'"
        )
        event = ClaimInspectionReadyEvent(
            event_id=EVENT_ID,
            event_type="claim.inspection.ready",
            claim_id=CLAIM_ID,
        )

        with self.assertRaises(ClaimEventProcessingError) as raised:
            await self.handler.handle(event)

        self.assertFalse(raised.exception.retryable)
        self.repository.complete_claim_event.assert_not_called()


class AdkClaimCoordinatorInvokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_submitted_event_stops_after_review_before_dispatch(self) -> None:
        tools = MagicMock()
        tools.get_claim_state.side_effect = [
            ClaimStateResult(claim_id=CLAIM_ID, status="intake_complete"),
            ClaimStateResult(claim_id=CLAIM_ID, status="inspection_pending"),
        ]
        tools.run_claim_review.return_value = ClaimStateResult(
            claim_id=CLAIM_ID,
            status="inspection_pending",
        )
        coordinator = build_firstnotice_coordinator(
            extraction_service=MagicMock(),
            workflow_tools=tools,
        )

        result = await AdkClaimCoordinatorInvoker(
            coordinator
        ).process_submitted_claim(CLAIM_ID)

        self.assertEqual(result["kind"], "review_completed")
        tools.run_claim_review.assert_called_once_with(CLAIM_ID)
        tools.dispatch_to_adjuster.assert_not_called()


class EventContractTests(unittest.TestCase):
    def test_unknown_event_type_is_rejected(self) -> None:
        data = submitted_event().model_dump(mode="json")
        data["event_type"] = "claim.unknown"

        with self.assertRaises(ValidationError):
            CLAIM_EVENT_ADAPTER.validate_python(data)

    def test_malformed_document_event_is_rejected(self) -> None:
        data = {
            "event_type": "claim.document.received",
            "event_id": EVENT_ID,
            "event_version": "1",
            "claim_id": CLAIM_ID,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "correlation_id": "corr-123",
            "source": "test",
            "payload": {},
        }

        with self.assertRaises(ValidationError):
            CLAIM_EVENT_ADAPTER.validate_python(data)

    def test_push_envelope_decoding_works(self) -> None:
        original = submitted_event()
        envelope = PubSubPushEnvelope.model_validate(push_body(original))

        decoded = decode_push_envelope(envelope)

        self.assertEqual(decoded, original)

    def test_malformed_json_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            parse_claim_event_json(b"{}")


class PubSubEndpointTests(unittest.TestCase):
    def test_successful_processing_returns_2xx(self) -> None:
        handler = MagicMock()
        handler.handle = AsyncMock(
            return_value=EventHandlingResult(
                event_id=EVENT_ID,
                event_type="claim.submitted",
                claim_id=CLAIM_ID,
                outcome="processed",
                claim_status="awaiting_documents",
            )
        )
        client = TestClient(create_app(handler))

        response = client.post("/events/pubsub", json=push_body(submitted_event()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "processed")

    def test_retryable_failure_returns_503(self) -> None:
        handler = MagicMock()
        handler.handle = AsyncMock(
            side_effect=ClaimEventProcessingError("temporary failure", retryable=True)
        )
        client = TestClient(create_app(handler))

        with patch("app.api.pubsub.logger.error") as log_exception:
            response = client.post("/events/pubsub", json=push_body(submitted_event()))

        self.assertEqual(response.status_code, 503)
        fields = log_exception.call_args.kwargs["extra"]
        self.assertEqual(fields["event_id"], EVENT_ID)
        self.assertEqual(fields["event_type"], "claim.submitted")
        self.assertEqual(fields["claim_id"], CLAIM_ID)
        self.assertEqual(fields["pubsub_message_id"], "message-123")
        self.assertEqual(fields["workflow_stage"], "unknown")
        self.assertTrue(log_exception.call_args.kwargs["exc_info"])

    def test_request_validation_logs_shape_without_raw_payload(self) -> None:
        client = TestClient(create_app(MagicMock()))

        with patch("app.api.pubsub.logger.warning") as log_warning:
            response = client.post(
                "/events/pubsub",
                json={"subscription": "sensitive-payload-must-not-be-logged"},
            )

        self.assertEqual(response.status_code, 422)
        fields = log_warning.call_args.kwargs["extra"]
        self.assertEqual(fields["workflow_stage"], "request_validation")
        self.assertEqual(fields["request_path"], "/events/pubsub")
        self.assertEqual(fields["validation_errors"][0]["location"], "body.message")
        self.assertNotIn("sensitive-payload", str(log_warning.call_args))

    def test_invalid_base64_returns_400(self) -> None:
        client = TestClient(create_app(MagicMock()))

        response = client.post(
            "/events/pubsub",
            json={"message": {"data": "%%%", "messageId": "message-123"}},
        )

        self.assertEqual(response.status_code, 400)


class PublisherTests(unittest.TestCase):
    def test_publisher_encodes_event_and_returns_message_id(self) -> None:
        client = MagicMock()
        client.topic_path.return_value = "projects/demo/topics/claims"
        future = MagicMock()
        future.result.return_value = "message-123"
        client.publish.return_value = future
        publisher = ClaimEventPublisher(
            PubSubSettings("demo", "claims"), client=client
        )

        message_id = publisher.publish(submitted_event())

        self.assertEqual(message_id, "message-123")
        call = client.publish.call_args
        self.assertEqual(call.args[0], "projects/demo/topics/claims")
        payload = json.loads(call.args[1])
        self.assertEqual(payload["event_type"], "claim.submitted")
        self.assertEqual(call.kwargs["event_id"], EVENT_ID)


class ProcessedEventRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock()
        self.claims = MagicMock()
        self.claim_ref = MagicMock()
        self.processed_collection = MagicMock()
        self.processed_ref = MagicMock()
        self.events_collection = MagicMock()
        self.timeline_ref = MagicMock()
        self.batch = MagicMock()
        self.client.collection.return_value = self.claims
        self.claims.document.return_value = self.claim_ref
        self.claim_ref.collection.side_effect = lambda name: {
            "processed_events": self.processed_collection,
            "events": self.events_collection,
        }[name]
        self.processed_collection.document.return_value = self.processed_ref
        self.events_collection.document.return_value = self.timeline_ref
        self.client.batch.return_value = self.batch
        self.repository = FirestoreClaimRepository(self.client)

    def test_begin_event_persists_processing_record_and_timeline(self) -> None:
        should_process = self.repository.begin_claim_event(
            CLAIM_ID,
            event_id=EVENT_ID,
            event_type="claim.submitted",
            event_version="1",
            occurred_at=datetime.now(timezone.utc),
            correlation_id="corr-123",
            source="test",
        )

        self.assertTrue(should_process)
        processed = self.batch.create.call_args_list[0].args[1]
        timeline = self.batch.create.call_args_list[1].args[1]
        self.assertEqual(processed["status"], "processing")
        self.assertEqual(processed["event_id"], EVENT_ID)
        self.assertEqual(timeline["action"], "pubsub_event_received")
        self.batch.commit.assert_called_once_with()

    def test_existing_processed_event_returns_duplicate_no_op(self) -> None:
        self.batch.commit.side_effect = AlreadyExists("already reserved")
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {"status": "processed"}
        self.processed_ref.get.return_value = snapshot

        should_process = self.repository.begin_claim_event(
            CLAIM_ID,
            event_id=EVENT_ID,
            event_type="claim.submitted",
            event_version="1",
            occurred_at=datetime.now(timezone.utc),
            correlation_id="corr-123",
            source="test",
        )

        self.assertFalse(should_process)
        duplicate = self.timeline_ref.create.call_args.args[0]
        self.assertEqual(duplicate["action"], "pubsub_event_duplicate")


if __name__ == "__main__":
    unittest.main()
