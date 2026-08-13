import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.firstnotice_adk import (
    CoordinatorAction,
    UnsupportedCoordinatorState,
    build_firstnotice_coordinator,
    route_claim_state,
)
from app.models.adk_orchestration import ClaimStateResult, EvidenceInput
from app.models.claim_document import ClaimDocument
from app.models.intake_result import (
    EvidenceArtifactClassification,
    EvidenceArtifactFacts,
    ImageEvidenceCapabilities,
    IntakeResult,
)
from app.models.review_result import ReviewResult
from app.tools.adk_workflow_tools import (
    ClaimWorkflowToolAdapter,
    build_initial_review_metadata,
)


def intake_result() -> IntakeResult:
    return IntakeResult(
        claim_type="auto_collision",
        damage_type="Rear bumper damage",
        parts_affected=["rear bumper"],
        incident_summary="The vehicle was struck from behind.",
        policy_number="POL-123",
        incident_date="2026-08-05",
        vehicle_drivable=True,
    )


class FakeExtractionService:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, paths: list[Path]) -> IntakeResult:
        self.calls += 1
        return intake_result()


class FakeWorkflowTools:
    def __init__(self, initial_status: str | None = None) -> None:
        self.claim_id = "CLM-ADK00001"
        self.status = initial_status
        self.review_calls = 0
        self.dispatch_calls = 0
        self.create_calls = 0

    def get_claim_state(self, claim_id: str) -> ClaimStateResult:
        if self.status is None:
            raise ValueError("Claim does not exist")
        return ClaimStateResult(claim_id=claim_id, status=self.status)

    def create_claim_record(self, intake, evidence) -> ClaimStateResult:
        self.create_calls += 1
        self.status = "intake_complete"
        return ClaimStateResult(claim_id=self.claim_id, status=self.status)

    def run_claim_review(self, claim_id: str) -> ClaimStateResult:
        self.review_calls += 1
        self.status = "awaiting_documents"
        return ClaimStateResult(
            claim_id=claim_id,
            status=self.status,
            missing_documents=["license_plate_photo"],
        )

    def dispatch_to_adjuster(self, claim_id: str) -> dict[str, object]:
        self.dispatch_calls += 1
        self.status = "adjuster_notified"
        return {"claim_id": claim_id, "final_status": self.status}


async def run_agent(agent, state: dict[str, object]) -> list[dict[str, object]]:
    sessions = InMemorySessionService()
    await sessions.create_session(
        app_name="agents",
        user_id="test-user",
        session_id="test-session",
        state=state,
    )
    runner = Runner(
        app_name="agents",
        agent=agent,
        session_service=sessions,
    )
    outputs = []
    async for event in runner.run_async(
        user_id="test-user",
        session_id="test-session",
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="Run workflow")]
        ),
    ):
        if isinstance(event.output, dict):
            outputs.append(event.output)
    return outputs


class CoordinatorRoutingTests(unittest.TestCase):
    def test_new_claim_routes_to_intake(self) -> None:
        self.assertEqual(route_claim_state("new"), CoordinatorAction.RUN_INTAKE)

    def test_intake_complete_routes_to_review(self) -> None:
        self.assertEqual(
            route_claim_state("intake_complete"), CoordinatorAction.RUN_REVIEW
        )

    def test_review_processing_retries_review(self) -> None:
        self.assertEqual(
            route_claim_state("review_processing"), CoordinatorAction.RUN_REVIEW
        )

    def test_awaiting_documents_stops(self) -> None:
        self.assertEqual(
            route_claim_state("awaiting_documents"),
            CoordinatorAction.WAIT_FOR_EVIDENCE,
        )

    def test_inspection_pending_routes_to_dispatch(self) -> None:
        self.assertEqual(
            route_claim_state("inspection_pending"),
            CoordinatorAction.DISPATCH_CLAIM,
        )

    def test_human_review_required_stops(self) -> None:
        self.assertEqual(
            route_claim_state("human_review_required"),
            CoordinatorAction.STOP_FOR_HUMAN,
        )

    def test_adjuster_notified_stops_complete(self) -> None:
        self.assertEqual(
            route_claim_state("adjuster_notified"), CoordinatorAction.COMPLETE
        )

    def test_invalid_state_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedCoordinatorState):
            route_claim_state("made_up_by_model")


class AdkRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_claim_runs_intake_then_review_and_waits(self) -> None:
        tools = FakeWorkflowTools()
        extraction = FakeExtractionService()
        coordinator = build_firstnotice_coordinator(
            extraction_service=extraction,
            workflow_tools=tools,
        )

        outputs = await run_agent(
            coordinator,
            {
                "new_claim": True,
                "evidence_inputs": [
                    {"path": "/demo/photo.jpg", "document_type": "damage_evidence"}
                ],
            },
        )

        self.assertEqual(extraction.calls, 1)
        self.assertEqual(tools.create_calls, 1)
        self.assertEqual(tools.review_calls, 1)
        final = next(item for item in outputs if item.get("kind") == "coordinator_result")
        self.assertEqual(final["result"]["final_status"], "awaiting_documents")

    async def test_awaiting_documents_stops_without_downstream_calls(self) -> None:
        tools = FakeWorkflowTools("awaiting_documents")
        coordinator = build_firstnotice_coordinator(
            extraction_service=FakeExtractionService(), workflow_tools=tools
        )

        outputs = await run_agent(coordinator, {"claim_id": tools.claim_id})

        self.assertEqual(tools.review_calls, 0)
        self.assertEqual(tools.dispatch_calls, 0)
        final = next(item for item in outputs if item.get("kind") == "coordinator_result")
        self.assertEqual(
            final["result"]["stop_reason"], "awaiting_external_evidence"
        )

    async def test_review_processing_resumes_review_after_retry(self) -> None:
        tools = FakeWorkflowTools("review_processing")
        coordinator = build_firstnotice_coordinator(
            extraction_service=FakeExtractionService(), workflow_tools=tools
        )

        outputs = await run_agent(
            coordinator,
            {"claim_id": tools.claim_id, "stop_after_review": True},
        )

        self.assertEqual(tools.review_calls, 1)
        final = next(item for item in outputs if item.get("kind") == "coordinator_result")
        self.assertEqual(final["result"]["final_status"], "awaiting_documents")

    async def test_duplicate_coordinator_execution_does_not_duplicate_dispatch(self) -> None:
        tools = FakeWorkflowTools("inspection_pending")
        coordinator = build_firstnotice_coordinator(
            extraction_service=FakeExtractionService(), workflow_tools=tools
        )

        first = await run_agent(coordinator, {"claim_id": tools.claim_id})
        second = await run_agent(coordinator, {"claim_id": tools.claim_id})

        self.assertEqual(tools.dispatch_calls, 1)
        self.assertTrue(
            any(item.get("kind") == "coordinator_result" for item in first)
        )
        second_final = next(
            item for item in second if item.get("kind") == "coordinator_result"
        )
        self.assertEqual(second_final["result"]["final_status"], "adjuster_notified")


class AdkToolAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.review_service = MagicMock()
        self.resume_workflow = MagicMock()
        self.dispatch_workflow = MagicMock()
        self.adapter = ClaimWorkflowToolAdapter(
            repository=self.repository,
            review_service=self.review_service,
            resume_workflow=self.resume_workflow,
            dispatch_workflow=self.dispatch_workflow,
        )

    def test_function_tools_expose_existing_capabilities(self) -> None:
        names = {tool.name for tool in self.adapter.function_tools()}

        self.assertEqual(
            names,
            {
                "get_claim_state",
                "create_claim_record",
                "run_claim_review",
                "request_missing_evidence",
                "resume_claim_with_document",
                "dispatch_to_adjuster",
            },
        )

    def test_create_claim_adapter_uses_existing_repository(self) -> None:
        self.repository.save_completed_intake.return_value = "CLM-ADK00001"
        self.repository.get_claim.return_value = {
            "claim_id": "CLM-ADK00001",
            "status": "intake_complete",
        }

        state = self.adapter.create_claim_record(
            intake_result(),
            [EvidenceInput(path="/demo/photo.jpg", document_type="damage_evidence")],
        )

        self.repository.save_completed_intake.assert_called_once()
        self.repository.add_document.assert_called_once()
        self.assertEqual(state.status, "intake_complete")

    def test_content_classification_is_authoritative_before_review(self) -> None:
        documents = [
            ClaimDocument(
                document_id="DOC-PNG-REPORT",
                claim_id="CLM-ADK00001",
                document_type="damage_evidence",
                filename="police-report.png",
                content_type="image/png",
                received_at=datetime.now(timezone.utc),
            ),
            ClaimDocument(
                document_id="DOC-PDF-REPORT",
                claim_id="CLM-ADK00001",
                document_type="police_report",
                filename="official-report.pdf",
                content_type="application/pdf",
                received_at=datetime.now(timezone.utc),
            ),
            ClaimDocument(
                document_id="DOC-JPG-REPORT",
                claim_id="CLM-ADK00001",
                document_type="damage_evidence",
                filename="incident-report.jpg",
                content_type="image/jpeg",
                received_at=datetime.now(timezone.utc),
            ),
            ClaimDocument(
                document_id="DOC-DAMAGE",
                claim_id="CLM-ADK00001",
                document_type="damage_evidence",
                filename="vehicle-damage.jpg",
                content_type="image/jpeg",
                received_at=datetime.now(timezone.utc),
            ),
            ClaimDocument(
                document_id="DOC-POLICY",
                claim_id="CLM-ADK00001",
                document_type="police_report",
                filename="policy-card.pdf",
                content_type="application/pdf",
                received_at=datetime.now(timezone.utc),
            ),
        ]
        result = intake_result().model_copy(update={
            "evidence_artifact_classifications": [
                EvidenceArtifactClassification(
                    source="police-report.png", document_type="police_report"
                ),
                EvidenceArtifactClassification(
                    source="official-report.pdf", document_type="police_report"
                ),
                EvidenceArtifactClassification(
                    source="incident-report.jpg", document_type="police_report"
                ),
                EvidenceArtifactClassification(
                    source="vehicle-damage.jpg", document_type="damage_evidence"
                ),
                EvidenceArtifactClassification(
                    source="policy-card.pdf", document_type="policy_document"
                ),
            ],
            "image_evidence_capabilities": [ImageEvidenceCapabilities(
                source="vehicle-damage.jpg",
                supported_capabilities=[
                    "damage_evidence", "vehicle_identity", "license_plate_photo"
                ],
            )],
            "evidence_artifact_facts": [
                EvidenceArtifactFacts(
                    source="policy-card.pdf",
                    policy_number="POL-12345",
                    vehicle_identity="2024 Example Sedan",
                    vehicle_make="Example",
                    vehicle_model="Sedan",
                    vehicle_year="2024",
                ),
                EvidenceArtifactFacts(
                    source="official-report.pdf",
                    vehicle_identity="2024 Example Sedan",
                    vehicle_make="Example",
                    vehicle_model="Sedan",
                    incident_date="2026-08-01",
                ),
                EvidenceArtifactFacts(
                    source="vehicle-damage.jpg",
                    license_plate="ABC123",
                    damage_location="rear",
                ),
            ],
        })
        expected_evidence_updates = {
            "DOC-DAMAGE": {
                "evidence_facts": {
                    "license_plate": "ABC123",
                    "damage_location": "rear",
                },
                "evidence_findings": [
                    "damage_location: rear",
                    "license_plate: ABC123",
                ],
            },
            "DOC-PDF-REPORT": {
                "evidence_facts": {
                    "vehicle_identity": "2024 Example Sedan",
                    "vehicle_make": "Example",
                    "vehicle_model": "Sedan",
                    "incident_date": "2026-08-01",
                },
                "evidence_findings": [
                    "incident_date: 2026-08-01",
                    "vehicle_identity: 2024 Example Sedan",
                    "vehicle_make: Example",
                    "vehicle_model: Sedan",
                ],
            },
            "DOC-POLICY": {
                "evidence_facts": {
                    "policy_number": "POL-12345",
                    "vehicle_identity": "2024 Example Sedan",
                    "vehicle_make": "Example",
                    "vehicle_model": "Sedan",
                    "vehicle_year": "2024",
                },
                "evidence_findings": [
                    "policy_number: POL-12345",
                    "vehicle_identity: 2024 Example Sedan",
                    "vehicle_make: Example",
                    "vehicle_model: Sedan",
                    "vehicle_year: 2024",
                ],
            },
        }
        self.repository.get_documents.return_value = documents
        self.repository.get_claim.return_value = {
            "claim_id": "CLM-ADK00001",
            "status": "intake_complete",
        }

        state = self.adapter.complete_claim_intake("CLM-ADK00001", result)

        self.repository.complete_claim_shell_intake.assert_called_once_with(
            "CLM-ADK00001",
            result,
            document_type_updates={
                "DOC-PNG-REPORT": "police_report",
                "DOC-PDF-REPORT": "police_report",
                "DOC-JPG-REPORT": "police_report",
                "DOC-DAMAGE": "damage_evidence",
                "DOC-POLICY": "policy_document",
            },
            document_evidence_updates=expected_evidence_updates,
        )
        self.assertEqual(state.status, "intake_complete")
        authoritative_types = {
            "DOC-PNG-REPORT": "police_report",
            "DOC-PDF-REPORT": "police_report",
            "DOC-JPG-REPORT": "police_report",
            "DOC-DAMAGE": "damage_evidence",
            "DOC-POLICY": "policy_document",
        }
        review_metadata = build_initial_review_metadata(
            result,
            [
                item.model_copy(
                    update={
                        "document_type": authoritative_types[item.document_id],
                        **expected_evidence_updates.get(item.document_id, {}),
                    }
                )
                for item in documents
            ],
        )
        damage_types = {
            item.evidence_type
            for item in review_metadata.uploaded_evidence
            if item.filename == "vehicle-damage.jpg"
        }
        self.assertEqual(
            damage_types,
            {"damage_evidence", "vehicle_identity", "license_plate_photo"},
        )
        self.assertIn(
            "policy_document",
            {
                item.evidence_type
                for item in review_metadata.uploaded_evidence
                if item.filename == "policy-card.pdf"
            },
        )
        policy_evidence = next(
            item
            for item in review_metadata.uploaded_evidence
            if item.filename == "policy-card.pdf"
        )
        self.assertIn("vehicle_identity: 2024 Example Sedan", policy_evidence.evidence_findings)
        image_evidence = next(
            item
            for item in review_metadata.uploaded_evidence
            if item.filename == "vehicle-damage.jpg"
        )
        self.assertNotIn("policy_number: POL-12345", image_evidence.evidence_findings)

    def test_review_adapter_calls_existing_review_service(self) -> None:
        initial_claim = {
            "claim_id": "CLM-ADK00001",
            "status": "intake_complete",
            **intake_result().model_dump(mode="python"),
        }
        reviewed_claim = {
            **initial_claim,
            "status": "awaiting_documents",
            "missing_documents": [{"type": "license_plate_photo"}],
        }
        self.repository.get_claim.side_effect = [initial_claim, reviewed_claim]
        self.repository.get_documents.return_value = []
        self.review_service.review.return_value = ReviewResult(
            intake_complete=False,
            intake_priority="routine",
            priority_reason="Missing plate photo.",
            confidence=0.9,
            inspection_required=True,
            missing_documents=[],
            requires_human_review=False,
        )

        state = self.adapter.run_claim_review("CLM-ADK00001")

        self.repository.update_claim_status.assert_called_once_with(
            "CLM-ADK00001", "review_processing"
        )
        self.review_service.review.assert_called_once()
        self.repository.save_review_result.assert_called_once()
        self.assertEqual(state.status, "awaiting_documents")

    def test_review_adapter_retries_after_failure_without_repeating_transition(self) -> None:
        initial_claim = {
            "claim_id": "CLM-ADK00001",
            "status": "intake_complete",
            **intake_result().model_dump(mode="python"),
        }
        processing_claim = {**initial_claim, "status": "review_processing"}
        ready_claim = {**initial_claim, "status": "inspection_ready"}
        self.repository.get_claim.side_effect = [
            initial_claim,
            processing_claim,
            ready_claim,
        ]
        self.repository.get_documents.return_value = []
        completed_review = ReviewResult(
            intake_complete=True,
            intake_priority="routine",
            priority_reason="Complete evidence.",
            confidence=0.95,
            inspection_required=True,
            missing_documents=[],
            requires_human_review=False,
        )
        self.review_service.review.side_effect = [
            RuntimeError("temporary review failure"),
            completed_review,
        ]

        with self.assertRaisesRegex(RuntimeError, "temporary review failure"):
            self.adapter.run_claim_review("CLM-ADK00001")
        state = self.adapter.run_claim_review("CLM-ADK00001")

        self.repository.update_claim_status.assert_called_once_with(
            "CLM-ADK00001", "review_processing"
        )
        self.assertEqual(self.review_service.review.call_count, 2)
        self.repository.save_review_result.assert_called_once_with(
            "CLM-ADK00001",
            completed_review,
            review_generation_key="CLM-ADK00001:submitted-review:v1",
        )
        self.assertEqual(state.status, "inspection_ready")

    def test_resume_and_dispatch_adapters_delegate_without_duplication(self) -> None:
        self.resume_workflow.resume.return_value.model_dump.return_value = {
            "final_status": "inspection_pending"
        }
        self.dispatch_workflow.dispatch.return_value.model_dump.return_value = {
            "final_status": "adjuster_notified"
        }
        document = MagicMock()

        resume_result = self.adapter.resume_claim_with_document(
            "CLM-ADK00001", document
        )
        dispatch_result = self.adapter.dispatch_to_adjuster("CLM-ADK00001")

        self.resume_workflow.resume.assert_called_once_with(
            "CLM-ADK00001", document
        )
        self.dispatch_workflow.dispatch.assert_called_once_with("CLM-ADK00001")
        self.assertEqual(resume_result["final_status"], "inspection_pending")
        self.assertEqual(dispatch_result["final_status"], "adjuster_notified")


if __name__ == "__main__":
    unittest.main()
