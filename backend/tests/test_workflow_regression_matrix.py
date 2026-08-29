import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.domain.claim_status import ClaimStatus, review_target_status, validate_claim_status_transition
from app.domain.evidence_reasoning import shape_source_aware_conflicts
from app.models.adjuster_packet import AdjusterNotificationDraft
from app.models.claim_document import ClaimDocument, DocumentExtractionResult
from app.models.intake_result import EvidenceArtifactFacts
from app.models.notification import AdjusterNotification
from app.models.requested_action import (
    EnterTextRequestedAction,
    UploadDocumentRequestedAction,
    parse_requested_actions,
)
from app.models.review_result import (
    CurrentEvidenceFinding,
    EvidenceConflict,
    MissingEvidence,
    OperationalIndicators,
    ReviewResult,
    UnresolvedUncertainty,
    UploadedEvidence,
)
from app.services.adjuster_dispatch_service import AdjusterDispatchService
from app.services.claim_review_service import ClaimReviewError
from app.services.claim_review_service import ClaimReviewService
from app.services.claim_submission_service import build_claimant_evidence_requests
from app.services.inspection_scheduling_service import InspectionSchedulingService
from app.tools.adk_workflow_tools import ClaimWorkflowToolAdapter
from app.workflows.claim_dispatch_workflow import ClaimDispatchWorkflow
from app.workflows.claim_resume_workflow import ClaimResumeWorkflow


CLAIM_ID = "CLM-MATRIX01"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def complete_review(*, findings: list[CurrentEvidenceFinding] | None = None) -> ReviewResult:
    return ReviewResult(
        intake_complete=True,
        intake_priority="routine",
        priority_reason="Current evidence is complete and operationally routine.",
        confidence=0.95,
        inspection_required=True,
        requires_human_review=False,
        current_evidence_findings=findings or [],
        operational_indicators=OperationalIndicators(),
    )


def identity_missing_review(
    *, action: UploadDocumentRequestedAction | None = None,
    findings: list[CurrentEvidenceFinding] | None = None,
) -> ReviewResult:
    return ReviewResult(
        intake_complete=False,
        intake_priority="routine",
        priority_reason="Routine claimant evidence can resolve vehicle identity.",
        confidence=0.82,
        inspection_required=True,
        requires_human_review=False,
        missing_documents=[
            MissingEvidence(
                type="vehicle_identity",
                reason="Vehicle identity is not yet readable.",
                source_requirement="always_required",
            ),
            MissingEvidence(
                type="license_plate_photo",
                reason="A readable license plate photo is required.",
                source_requirement="license_plate_photo",
            ),
        ],
        requested_actions=[action] if action else [],
        current_evidence_findings=findings or [],
        operational_indicators=OperationalIndicators(),
    )


def auto_reconciliation_review() -> ReviewResult:
    conflict = EvidenceConflict(
        field="damage_location",
        values=["rear", "front", "rear"],
        sources=["police-report.pdf", "bad-front.jpg", "correct-rear-plate.jpg"],
        reason="One image conflicts with corroborated current evidence.",
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
    uploaded = [
        UploadedEvidence(
            evidence_type="police_report", filename="police-report.pdf",
            document_id="DOC-REPORT", source_identity="document:DOC-REPORT",
            document_type="police_report",
            evidence_findings=["Rear-end collision with rear damage."],
        ),
        UploadedEvidence(
            evidence_type="damage_evidence", filename="bad-front.jpg",
            document_id="DOC-BAD", source_identity="document:DOC-BAD",
            document_type="license_plate_photo",
            evidence_findings=["A different silver SUV has front damage."],
        ),
        UploadedEvidence(
            evidence_type="damage_evidence", filename="correct-rear-plate.jpg",
            document_id="DOC-CURRENT", source_identity="document:DOC-CURRENT",
            document_type="license_plate_photo",
            evidence_findings=["The identified grey sedan has rear damage."],
        ),
        UploadedEvidence(
            evidence_type="vehicle_identity", filename="correct-rear-plate.jpg",
            document_id="DOC-CURRENT", source_identity="document:DOC-CURRENT",
            document_type="license_plate_photo",
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
            [conflict], findings, uploaded
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


class ScriptedReviewService:
    """Deterministic stand-in for Gemini at the production review boundary."""

    def __init__(self) -> None:
        self.responses: list[ReviewResult | Exception] = []
        self.active_sources: list[set[str]] = []
        self.intake_contexts = []

    def queue(self, *responses: ReviewResult | Exception) -> None:
        self.responses.extend(responses)

    def review(self, intake, metadata, *, evidence_parts):
        self.intake_contexts.append(intake)
        self.active_sources.append(
            {item.filename for item in metadata.uploaded_evidence}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MatrixStateRepository:
    """In-memory repository contract; all routing remains in production workflows."""

    def __init__(self, documents: list[ClaimDocument]) -> None:
        self.claim = {
            "claim_id": CLAIM_ID,
            "status": "intake_complete",
            "claim_type": "auto_collision",
            "damage_type": "Rear bumper damage",
            "parts_affected": ["rear bumper", "left tail light"],
            "incident_summary": "The insured vehicle was struck from behind.",
            "incident_description": "The insured vehicle was struck from behind.",
            "policy_number": "POL-DEMO-1001",
            "incident_date": "2026-08-05",
            "vehicle_drivable": True,
            "uncertainties": [],
            "image_evidence_capabilities": [],
            "missing_documents": [],
            "unusable_evidence": [],
            "requested_actions": [],
            "operational_indicators": {},
            "current_evidence_findings": [],
            "source_aware_conflicts": [],
            "source_aware_uncertainties": [],
            "unresolved_uncertainties": [],
        }
        self.documents = {item.document_id: item for item in documents}
        self.events: dict[str, dict[str, object]] = {}
        self.review_generation_keys: set[str] = set()
        self.decision_notification_intents = 0
        self.appointment = None
        self.notification = None
        self.create_document_calls = 0
        self.supersession_writes = 0
        self.action_consumptions = 0

    def get_claim(self, claim_id):
        return dict(self.claim)

    def get_documents(self, claim_id):
        return list(self.documents.values())

    def get_document(self, claim_id, document_id):
        return self.documents.get(document_id)

    def add_document(self, document):
        if document.document_id in self.documents:
            raise AssertionError("resume attempted to recreate an existing document")
        self.create_document_calls += 1
        self.documents[document.document_id] = document

    def update_claim_status(self, claim_id, status):
        target = ClaimStatus(status)
        validate_claim_status_transition(self.claim["status"], target)
        self.claim["status"] = target.value
        return target

    def begin_document_resume_review(
        self, claim_id, document_id, *, idempotency_key, matched_requirement,
        correlation_id, replacement_action_id=None, replaces_document_id=None,
        replacement_document_type=None,
    ):
        validate_claim_status_transition(self.claim["status"], ClaimStatus.REVIEW_PROCESSING)
        document = self.documents[document_id]
        if replacement_action_id:
            action = next(
                item for item in parse_requested_actions(self.claim["requested_actions"])
                if item.action_id == replacement_action_id
            )
            if (
                not isinstance(action, UploadDocumentRequestedAction)
                or action.replaces_document_id != replaces_document_id
                or action.document_type != replacement_document_type
                or self.documents[replaces_document_id].status == "superseded"
            ):
                raise AssertionError("invalid server replacement binding")
            document = document.model_copy(update={
                "requested_action_id": action.action_id,
                "replaces_document_id": action.replaces_document_id,
                "document_type": action.document_type,
            })
        self.documents[document_id] = document.model_copy(update={
            "resume_idempotency_key": idempotency_key,
            "resume_correlation_id": correlation_id,
            "resume_matched_requirement": matched_requirement,
            "resume_started_at": NOW,
        })
        self.claim.update({
            "status": "review_processing",
            "active_resume_document_id": document_id,
            "active_resume_idempotency_key": idempotency_key,
            "active_resume_correlation_id": correlation_id,
        })
        return True

    def save_document_resume_extraction(self, claim_id, document_id, extraction):
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={"resume_extraction_result": extraction}
        )

    def mark_document_resume_quality_processed(self, claim_id, document_id):
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={"resume_quality_processed_at": NOW}
        )

    def mark_document_validated(self, claim_id, document_id, **values):
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={"status": "validated", **values}
        )

    def mark_document_unusable(self, claim_id, document_id, reason, **values):
        self.documents[document_id] = self.documents[document_id].model_copy(
            update={"status": "unusable", "quality_reason": reason, **values}
        )

    def mark_document_resume_processed(
        self, claim_id, document_id, *, idempotency_key, result_status
    ):
        self.documents[document_id] = self.documents[document_id].model_copy(update={
            "resume_idempotency_key": idempotency_key,
            "resume_processed_at": NOW,
            "resume_result_status": result_status,
        })

    def mark_document_resume_retry_required(
        self, claim_id, document_id, *, error_type
    ):
        self.documents[document_id] = self.documents[document_id].model_copy(update={
            "resume_result_status": "retry_required",
            "resume_error": f"{error_type}: document resume failed; retry is required.",
            "resume_retry_required_at": NOW,
        })

    def complete_requested_evidence_item(
        self, *, claim_id, document, extraction, remaining_actions,
        idempotency_key, retry_action=None,
    ):
        persisted_actions = [
            action
            for action in parse_requested_actions(self.claim["requested_actions"])
            if isinstance(action, UploadDocumentRequestedAction)
        ]
        actions = remaining_actions if extraction.usable else [
            (
                retry_action
                if retry_action is not None
                and action.action_id == retry_action.action_id
                else action
            )
            for action in persisted_actions
        ]
        self.claim.update({
            "status": "awaiting_documents",
            "requested_actions": [
                item.model_dump(mode="python") for item in actions
            ],
            "missing_documents": [
                {"type": item.document_type, "reason": item.instruction}
                for item in actions
            ],
        })
        for key in [
            "active_resume_document_id",
            "active_resume_idempotency_key",
            "active_resume_correlation_id",
        ]:
            self.claim.pop(key, None)
        persisted = self.documents[document.document_id]
        self.documents[document.document_id] = persisted.model_copy(update={
            "status": "validated" if extraction.usable else "unusable",
            "resume_idempotency_key": idempotency_key,
            "resume_processed_at": NOW,
            "resume_result_status": "awaiting_documents",
        })

    def append_claim_event(self, claim_id, *, event_id=None, **values):
        key = event_id or f"event-{len(self.events) + 1}"
        self.events.setdefault(key, values)
        return key

    def save_review_result(
        self, claim_id, result, *, resume_document_id=None,
        resume_idempotency_key=None, replacement_document=None,
        retry_replacement_action_id=None, review_generation_key=None, **kwargs,
    ):
        target = review_target_status(result)
        validate_claim_status_transition(self.claim["status"], target)
        self.claim.update({
            "status": target.value,
            "review_status": "completed",
            "intake_complete": result.intake_complete,
            "intake_priority": result.intake_priority,
            "priority_reason": result.priority_reason,
            "review_confidence": result.confidence,
            "inspection_required": result.inspection_required,
            "requires_human_review": result.requires_human_review,
            "human_review_reason": result.human_review_reason,
            "missing_documents": [item.model_dump(mode="python") for item in result.missing_documents],
            "unusable_evidence": [item.model_dump(mode="python") for item in result.unusable_evidence],
            "conflicts": [item.model_dump(mode="python") for item in result.conflicts],
            "source_aware_conflicts": [item.model_dump(mode="python") for item in result.source_aware_conflicts],
            "source_aware_uncertainties": [item.model_dump(mode="python") for item in result.source_aware_uncertainties],
            "unresolved_uncertainties": [item.model_dump(mode="python") for item in result.unresolved_uncertainties],
            "current_evidence_findings": [item.model_dump(mode="python") for item in result.current_evidence_findings],
            "operational_indicators": result.operational_indicators.model_dump(mode="python"),
            "requested_actions": [item.model_dump(mode="python") for item in result.requested_actions],
        })
        if target == ClaimStatus.INSPECTION_READY:
            key = review_generation_key or f"{claim_id}:submitted-review:v1"
            if key not in self.review_generation_keys:
                self.review_generation_keys.add(key)
                self.decision_notification_intents += 1
        if replacement_document is not None:
            current = self.documents[replacement_document.document_id]
            old = self.documents[replacement_document.replaces_document_id]
            if old.status != "superseded":
                self.supersession_writes += 1
            self.documents[old.document_id] = old.model_copy(update={
                "status": "superseded",
                "superseded_by_document_id": current.document_id,
            })
            self.documents[current.document_id] = replacement_document.model_copy(
                update={"status": "validated"}
            )
            self.action_consumptions += 1
        if retry_replacement_action_id:
            self.claim["requested_actions"] = [
                item.model_dump(mode="python")
                for item in parse_requested_actions(self.claim["requested_actions"])
            ]
        if resume_document_id:
            self.documents[resume_document_id] = self.documents[resume_document_id].model_copy(update={
                "resume_idempotency_key": resume_idempotency_key,
                "resume_processed_at": NOW,
                "resume_result_status": target.value,
            })
            for key in ["active_resume_document_id", "active_resume_idempotency_key", "active_resume_correlation_id"]:
                self.claim.pop(key, None)
        return target

    def get_appointment(self, claim_id, appointment_id):
        return self.appointment

    def get_notification(self, claim_id, notification_id):
        return self.notification

    def schedule_inspection(self, appointment, slots, **kwargs):
        self.appointment = appointment
        self.claim["status"] = "inspection_scheduled"

    def complete_adjuster_dispatch(self, **kwargs):
        self.claim.update({
            "status": "adjuster_notified",
            "dispatch_idempotency_key": kwargs["dispatch_idempotency_key"],
            "adjuster_packet": kwargs["packet"].model_dump(mode="python"),
        })


class WorkflowMatrixHarness:
    def __init__(self, documents: list[ClaimDocument]) -> None:
        self.repository = MatrixStateRepository(documents)
        self.review = ScriptedReviewService()
        self.extractor = MagicMock()
        self.resume = ClaimResumeWorkflow(
            repository=self.repository,
            review_service=self.review,
            document_extractor=self.extractor,
        )
        self.adapter = ClaimWorkflowToolAdapter(
            repository=self.repository,
            review_service=self.review,
            resume_workflow=self.resume,
            dispatch_workflow=MagicMock(),
        )
        self.calendar = MagicMock()
        self.calendar.create_inspection_event.return_value = SimpleNamespace(
            calendar_event_id="calendar-1",
            calendar_event_link="https://calendar.example/event-1",
            calendar_id="demo-calendar",
            created_at=NOW,
        )
        genai = MagicMock()
        genai.models.generate_content.return_value.text = AdjusterNotificationDraft(
            subject="Inspection scheduled",
            message="Review the current evidence and inspection.",
            action_requested="Review the adjuster packet.",
        ).model_dump_json()
        self.final_gmail = MagicMock()

        def notify(**kwargs):
            if self.repository.notification is None:
                self.repository.notification = AdjusterNotification(
                    notification_id=kwargs["notification_id"],
                    claim_id=CLAIM_ID,
                    subject=kwargs["draft"].subject,
                    message=kwargs["draft"].message,
                    action_requested=kwargs["draft"].action_requested,
                    created_at=kwargs["now"],
                    idempotency_key=kwargs["idempotency_key"],
                )
                self.final_gmail()
            return self.repository.notification

        notifier = MagicMock()
        notifier.send_adjuster_notification.side_effect = notify
        self.dispatch = ClaimDispatchWorkflow(
            repository=self.repository,
            scheduling_service=InspectionSchedulingService(),
            adjuster_service=AdjusterDispatchService(genai, "mock-model"),
            notification_tool=notifier,
            calendar_service=self.calendar,
        )

    def submit(self, result: ReviewResult):
        self.review.queue(result)
        state = self.adapter.run_claim_review(CLAIM_ID)
        return state.status

    def deliver(
        self, document: ClaimDocument, extraction: DocumentExtractionResult,
        *reviews: ReviewResult | Exception,
    ):
        self.repository.documents.setdefault(document.document_id, document)
        self.extractor.extract.return_value = extraction
        self.review.queue(*reviews)
        return self.resume.resume(CLAIM_ID, document)

    def snapshot(self):
        active = sorted(
            item.document_id for item in self.repository.documents.values()
            if item.status != "superseded"
        )
        superseded = sorted(
            item.document_id for item in self.repository.documents.values()
            if item.status == "superseded"
        )
        actions = parse_requested_actions(self.repository.claim["requested_actions"])
        requested_evidence = (
            [] if actions else build_claimant_evidence_requests(
                self.repository.claim["missing_documents"],
                self.repository.claim["unusable_evidence"],
            )
        )
        return {
            "status": self.repository.claim["status"],
            "requires_human_review": self.repository.claim.get("requires_human_review", False),
            "active_document_ids": active,
            "superseded_document_ids": superseded,
            "requested_actions": [item.action_id for item in actions],
            "missing_documents": [item["type"] for item in self.repository.claim["missing_documents"]],
            "requested_evidence": [item.document_type for item in requested_evidence],
            "current_evidence_findings": self.repository.claim["current_evidence_findings"],
            "source_aware_conflicts": self.repository.claim["source_aware_conflicts"],
            "source_aware_uncertainties": self.repository.claim["source_aware_uncertainties"],
            "unresolved_uncertainties": self.repository.claim["unresolved_uncertainties"],
            "operational_indicators": self.repository.claim["operational_indicators"],
            "decision_generations": len(self.repository.review_generation_keys),
        }

    def approve_and_dispatch(self):
        self.repository.claim["status"] = "inspection_pending"
        first = self.dispatch.dispatch(CLAIM_ID, now=NOW)
        second = self.dispatch.dispatch(CLAIM_ID, now=NOW)
        return first, second


def doc(
    document_id: str,
    filename: str,
    document_type: str,
    *,
    capabilities=(),
    findings=(),
    facts=None,
    requested_action_id=None,
):
    return ClaimDocument(
        document_id=document_id,
        claim_id=CLAIM_ID,
        document_type=document_type,
        filename=filename,
        status="validated",
        supported_capabilities=list(capabilities),
        evidence_facts=dict(facts or {}),
        evidence_findings=list(findings),
        requested_action_id=requested_action_id,
        received_at=NOW,
    )


class FrozenWorkflowRegressionMatrixTests(unittest.TestCase):
    def _vehicle_then_voice_demo_path(self) -> WorkflowMatrixHarness:
        vehicle_action = UploadDocumentRequestedAction(
            action_id="ACT-DEMO-VEHICLE",
            review_id="AUTONOMOUS-DEMO-VEHICLE",
            document_type="license_plate_photo",
            instruction="Please upload a clear vehicle photo with a readable license plate.",
        )
        harness = WorkflowMatrixHarness([
            doc(
                "DOC-POLICY", "policy.pdf", "policy_document",
                facts={
                    "vehicle_make": "Toyota",
                    "vehicle_model": "Corolla",
                    "license_plate": "7ABX123",
                },
            ),
            doc(
                "DOC-INITIAL", "initial.jpg", "damage_evidence",
                capabilities=("damage_evidence",),
            ),
        ])
        harness.repository.claim.update({
            "incident_date": None,
            "incident_summary": "",
            "incident_description": "",
        })
        self.assertEqual(
            harness.submit(identity_missing_review(action=vehicle_action)),
            "awaiting_documents",
        )

        wrong = doc(
            "DOC-WRONG", "wrong-vehicle.jpg", "license_plate_photo",
            requested_action_id=vehicle_action.action_id,
        )
        wrong_result = harness.deliver(
            wrong,
            DocumentExtractionResult(
                usable=True,
                reason="The vehicle and plate are readable.",
                supported_capabilities=[
                    "damage_evidence", "license_plate_photo", "vehicle_identity"
                ],
                evidence_facts=EvidenceArtifactFacts(
                    source="wrong-vehicle.jpg",
                    vehicle_make="Honda",
                    vehicle_model="CR-V",
                    license_plate="8XYZ999",
                ),
            ),
        )
        self.assertEqual(wrong_result.final_status, "awaiting_documents")
        self.assertEqual(harness.repository.documents["DOC-WRONG"].status, "unusable")
        actions = parse_requested_actions(
            harness.repository.claim["requested_actions"]
        )
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], UploadDocumentRequestedAction)
        self.assertEqual(actions[0].replaces_document_id, "DOC-WRONG")
        self.assertEqual(harness.review.responses, [])

        voice_action = EnterTextRequestedAction(
            action_id="ACT-DEMO-VOICE",
            review_id="AUTONOMOUS-DEMO-VOICE",
            field_name="incident_information",
            instruction=(
                "Tell us when the accident happened and briefly what happened. "
                "You can also mention any injuries or other important details."
            ),
        )
        correct = doc(
            "DOC-CORRECT", "correct-vehicle.jpg", "license_plate_photo",
            requested_action_id=vehicle_action.action_id,
        )
        correct_result = harness.deliver(
            correct,
            DocumentExtractionResult(
                usable=True,
                reason="The insured vehicle and plate are readable.",
                supported_capabilities=[
                    "damage_evidence", "license_plate_photo", "vehicle_identity"
                ],
                evidence_facts=EvidenceArtifactFacts(
                    source="correct-vehicle.jpg",
                    vehicle_make="Toyota",
                    vehicle_model="Corolla",
                    license_plate="7ABX123",
                ),
            ),
            ReviewResult(
                intake_complete=False,
                intake_priority="routine",
                priority_reason="Incident information is still required.",
                confidence=0.9,
                inspection_required=True,
                requires_human_review=False,
                missing_documents=[
                    MissingEvidence(
                        type="incident_date",
                        reason="Incident date is required.",
                        source_requirement="always_required",
                    ),
                    MissingEvidence(
                        type="incident_description",
                        reason="Incident context is required.",
                        source_requirement="always_required",
                    ),
                ],
                requested_actions=[voice_action],
                operational_indicators=OperationalIndicators(),
            ),
        )
        self.assertEqual(correct_result.final_status, "awaiting_documents")
        self.assertEqual(harness.repository.documents["DOC-WRONG"].status, "superseded")
        self.assertEqual(
            harness.repository.documents["DOC-CORRECT"].replaces_document_id,
            "DOC-WRONG",
        )
        final_actions = parse_requested_actions(
            harness.repository.claim["requested_actions"]
        )
        self.assertEqual(final_actions, [voice_action])
        self.assertEqual(
            {item["type"] for item in harness.repository.claim["missing_documents"]},
            {"incident_date", "incident_description"},
        )
        return harness

    def test_final_demo_vehicle_mismatch_then_voice_injury_requires_human_review(self):
        harness = self._vehicle_then_voice_demo_path()
        harness.repository.claim["status"] = "review_processing"
        injury_review = complete_review().model_copy(update={
            "intake_complete": False,
            "intake_priority": "urgent_human_review",
            "priority_reason": "Possible injury requires prompt human review.",
            "requires_human_review": True,
            "human_review_reason": "Possible injury requires prompt human review.",
            "operational_indicators": OperationalIndicators(possible_injury=True),
        })

        target = harness.repository.save_review_result(
            CLAIM_ID, injury_review, review_generation_key="voice-injury"
        )

        self.assertEqual(target, ClaimStatus.HUMAN_REVIEW_REQUIRED)
        self.assertEqual(harness.repository.claim["status"], "human_review_required")

    def test_final_demo_vehicle_mismatch_then_voice_no_injury_continues(self):
        harness = self._vehicle_then_voice_demo_path()
        harness.repository.claim["status"] = "review_processing"

        target = harness.repository.save_review_result(
            CLAIM_ID, complete_review(), review_generation_key="voice-no-injury"
        )

        self.assertEqual(target, ClaimStatus.INSPECTION_READY)
        self.assertEqual(harness.repository.claim["status"], "inspection_ready")

    def test_flow_1_complete_initial_evidence_and_approved_dispatch(self):
        harness = WorkflowMatrixHarness([
            doc("DOC-REPORT", "police-report.pdf", "police_report"),
            doc("DOC-REAR", "rear-plate.jpg", "damage_evidence",
                capabilities=("damage_evidence", "license_plate_photo", "vehicle_identity")),
        ])
        self.assertEqual(harness.submit(complete_review()), "inspection_ready")
        snapshot = harness.snapshot()
        self.assertFalse(snapshot["requires_human_review"])
        self.assertEqual(snapshot["requested_actions"], [])
        self.assertEqual(snapshot["decision_generations"], 1)
        self.assertEqual(harness.calendar.call_count, 0)

        first, second = harness.approve_and_dispatch()
        self.assertEqual(first.final_status, "adjuster_notified")
        self.assertTrue(second.idempotent_replay)
        harness.calendar.create_inspection_event.assert_called_once()
        self.assertEqual(harness.final_gmail.call_count, 1)
        self.assertIsNotNone(harness.repository.appointment)

    def test_flow_2_missing_identity_then_current_evidence_resolves(self):
        harness = WorkflowMatrixHarness([
            doc("DOC-REPORT", "police-report.pdf", "police_report"),
            doc("DOC-REAR", "rear.jpg", "damage_evidence", capabilities=("damage_evidence",)),
        ])
        self.assertEqual(harness.submit(identity_missing_review()), "awaiting_documents")
        first = harness.snapshot()
        self.assertEqual(set(first["missing_documents"]), {"vehicle_identity", "license_plate_photo"})
        self.assertEqual(first["requested_evidence"], ["license_plate_photo"])
        self.assertEqual(first["decision_generations"], 0)

        followup = doc("DOC-PLATE", "rear-plate.jpg", "license_plate_photo")
        result = harness.deliver(
            followup,
            DocumentExtractionResult(
                usable=True, reason="Rear damage and identity are readable.",
                supported_capabilities=["damage_evidence", "license_plate_photo", "vehicle_identity"],
            ),
            complete_review(),
        )
        self.assertEqual(result.final_status, "inspection_ready")
        final = harness.snapshot()
        self.assertEqual(final["missing_documents"], [])
        self.assertEqual(final["superseded_document_ids"], [])
        self.assertEqual(final["decision_generations"], 1)

    def test_flow_3_wrong_front_image_is_explicitly_superseded(self):
        action = UploadDocumentRequestedAction(
            action_id="ACT-FLOW-3", review_id="AUTONOMOUS-FLOW-3",
            document_type="damage_evidence",
            instruction="Upload correct rear damage with a readable plate.",
            replaces_document_id="DOC-FRONT",
        )
        harness = WorkflowMatrixHarness([
            doc("DOC-REPORT", "police-report.pdf", "police_report"),
            doc("DOC-FRONT", "front.jpg", "damage_evidence",
                capabilities=("damage_evidence",),
                findings=("Front damage and steam from the radiator.",)),
        ])
        harness.repository.claim.update({
            "damage_type": "Severe front-end damage and steaming radiator.",
            "parts_affected": ["front bumper", "headlight", "hood", "radiator"],
            "incident_summary": "A silver SUV has severe front-end damage.",
            "uncertainties": ["The silver SUV identity is unclear."],
            "image_evidence_capabilities": [{
                "source": "front.jpg",
                "supported_capabilities": ["damage_evidence"],
                "unusable_capabilities": [],
                "quality_observations": ["Steam is visible."],
            }],
        })
        self.assertEqual(harness.submit(identity_missing_review(action=action)), "awaiting_documents")
        replacement = doc("DOC-CORRECT", "rear-plate.jpg", "license_plate_photo")
        provider = MagicMock()
        provider.models.generate_content.return_value.text = complete_review(findings=[
            CurrentEvidenceFinding(
                source="rear-plate.jpg",
                finding="Rear damage and plate are consistent with the report.",
            ),
        ]).model_dump_json()
        harness.resume._review_service = ClaimReviewService(provider, "mock-model")
        result = harness.deliver(
            replacement,
            DocumentExtractionResult(
                usable=True, reason="Correct rear damage and plate are readable.",
                supported_capabilities=["damage_evidence", "license_plate_photo", "vehicle_identity"],
                evidence_findings=["Rear damage on the identified vehicle."],
            ),
        )
        self.assertEqual(result.final_status, "inspection_ready")
        snapshot = harness.snapshot()
        self.assertEqual(snapshot["superseded_document_ids"], ["DOC-FRONT"])
        prompt = provider.models.generate_content.call_args.kwargs["contents"][0].parts[0].text
        self.assertNotIn("silver SUV", prompt)
        self.assertNotIn("steaming radiator", prompt)
        self.assertNotIn("front.jpg", prompt)
        self.assertIn("rear-plate.jpg", prompt)
        self.assertEqual(
            harness.repository.claim["incident_summary"],
            "A silver SUV has severe front-end damage.",
        )
        current_findings = harness.repository.claim["current_evidence_findings"]
        self.assertTrue(current_findings)
        self.assertEqual(
            {item["source"] for item in current_findings}, {"rear-plate.jpg"}
        )
        self.assertIn(
            {
                "source": "rear-plate.jpg",
                "finding": "Rear damage and plate are consistent with the report.",
            },
            current_findings,
        )
        self.assertFalse(snapshot["operational_indicators"]["safety_concern"])
        self.assertEqual(harness.repository.supersession_writes, 1)
        self.assertEqual(harness.repository.action_consumptions, 1)
        self.assertEqual(harness.repository.documents["DOC-CORRECT"].replaces_document_id, "DOC-FRONT")
        self.assertEqual(harness.repository.documents["DOC-FRONT"].superseded_by_document_id, "DOC-CORRECT")

    def test_flow_4_bad_followup_gets_claimant_remediation_then_resolves(self):
        harness = WorkflowMatrixHarness([
            doc(
                "DOC-REPORT", "police-report.pdf", "police_report",
                findings=(
                    "Rear-end collision with rear bumper and left tail light damage; "
                    "the vehicle was drivable and no injuries were reported.",
                ),
            ),
            doc(
                "DOC-REAR", "IMG_5418.png", "damage_evidence",
                capabilities=("damage_evidence",),
                findings=("Rear bumper and left tail light damage.",),
            ),
        ])
        harness.submit(identity_missing_review())
        bad = doc("DOC-BAD", "IMG_5420.png", "license_plate_photo")
        first = harness.deliver(
            bad,
            DocumentExtractionResult(
                usable=True,
                reason="Damage is visible, but the requested identity is not readable.",
                supported_capabilities=["damage_evidence"],
                evidence_findings=[
                    "A silver Honda SUV has front-end damage.",
                    "A material safety concern is visible on that vehicle.",
                ],
            ),
            identity_missing_review(),
        )
        self.assertEqual(first.final_status, "awaiting_documents")

        correct = doc("DOC-CORRECT", "IMG_5419.png", "license_plate_photo")
        provider = MagicMock()
        conflicted_review = ReviewResult(
            intake_complete=False,
            intake_priority="routine",
            priority_reason="Claimant evidence can resolve the discrepancy.",
            confidence=0.82,
            inspection_required=True,
            conflicts=[EvidenceConflict(
                field="vehicle_identity_and_damage_location",
                values=["silver SUV/front", "grey sedan/rear"],
                sources=["IMG_5420.png", "IMG_5419.png"],
                reason="The follow-up images show different vehicles and damage locations.",
            )],
            current_evidence_findings=[
                CurrentEvidenceFinding(
                    source="police-report.pdf",
                    finding="Rear-end collision; rear damage; drivable; no injuries.",
                ),
                CurrentEvidenceFinding(
                    source="IMG_5418.png",
                    finding="Rear bumper and left tail light damage.",
                ),
                CurrentEvidenceFinding(
                    source="IMG_5420.png",
                    finding="A silver Honda SUV has front damage and a safety concern.",
                ),
                CurrentEvidenceFinding(
                    source="IMG_5419.png",
                    finding="A grey sedan has rear damage and California plate 7ABX123.",
                ),
            ],
            unresolved_uncertainties=[UnresolvedUncertainty(
                uncertainty=(
                    "The silver front-damage vehicle may not be the same vehicle "
                    "as the identified grey rear-damage vehicle."
                ),
                sources=["IMG_5420.png", "IMG_5419.png"],
            )],
            requires_human_review=False,
            operational_indicators=OperationalIndicators(
                safety_concern=True,
                significant_damage=True,
                high_operational_uncertainty=True,
            ),
        )
        provider.models.generate_content.side_effect = [
            SimpleNamespace(text=conflicted_review.model_dump_json()),
            SimpleNamespace(text=complete_review(findings=[
                CurrentEvidenceFinding(
                    source="IMG_5419.png",
                    finding=(
                        "The identified grey sedan has rear damage and plate 7ABX123."
                    ),
                )
            ]).model_dump_json()),
        ]
        harness.resume._review_service = ClaimReviewService(provider, "mock-model")
        second = harness.deliver(
            correct,
            DocumentExtractionResult(
                usable=True,
                reason="Rear damage and vehicle identity are readable.",
                supported_capabilities=[
                    "damage_evidence", "license_plate_photo", "vehicle_identity"
                ],
                evidence_findings=[
                    "A grey sedan has rear damage and California plate 7ABX123."
                ],
            ),
        )
        self.assertEqual(second.final_status, "inspection_ready")
        self.assertFalse(harness.snapshot()["requires_human_review"])
        actions = parse_requested_actions(harness.repository.claim["requested_actions"])
        self.assertEqual(actions, [])
        snapshot = harness.snapshot()
        self.assertEqual(snapshot["superseded_document_ids"], ["DOC-BAD"])
        committed = harness.repository.documents["DOC-CORRECT"]
        self.assertEqual(committed.replaces_document_id, "DOC-BAD")
        self.assertIsNotNone(committed.requested_action_id)
        self.assertFalse(snapshot["operational_indicators"]["safety_concern"])
        self.assertEqual(harness.repository.supersession_writes, 1)
        self.assertEqual(snapshot["decision_generations"], 1)

    def test_flow_4_existing_explicit_second_replacement_remains_supported(self):
        action = UploadDocumentRequestedAction(
            action_id="ACT-FLOW-4-LEGACY",
            review_id="AUTONOMOUS-FLOW-4-LEGACY",
            document_type="damage_evidence",
            instruction="Upload the correct rear damage and plate image.",
            replaces_document_id="DOC-BAD",
        )
        harness = WorkflowMatrixHarness([
            doc("DOC-REPORT", "police-report.pdf", "police_report"),
            doc("DOC-BAD", "bad-front.jpg", "damage_evidence"),
        ])
        harness.submit(identity_missing_review(action=action))

        replacement = doc(
            "DOC-LEGACY-REPLACEMENT", "correct-rear-plate.jpg",
            "license_plate_photo",
        )
        result = harness.deliver(
            replacement,
            DocumentExtractionResult(
                usable=True,
                reason="Rear damage and the plate are readable.",
                supported_capabilities=[
                    "damage_evidence", "license_plate_photo", "vehicle_identity"
                ],
            ),
            complete_review(),
        )

        self.assertEqual(result.final_status, "inspection_ready")
        self.assertEqual(harness.snapshot()["superseded_document_ids"], ["DOC-BAD"])
        self.assertEqual(harness.repository.supersession_writes, 1)

    def test_flow_4_auto_reconciliation_retry_does_not_repeat_supersession(self):
        harness = WorkflowMatrixHarness([
            doc(
                "DOC-REPORT", "police-report.pdf", "police_report",
                findings=("Rear-end collision with rear damage.",),
            ),
            doc(
                "DOC-BAD", "bad-front.jpg", "license_plate_photo",
                capabilities=("damage_evidence",),
                findings=("A different silver SUV has front damage.",),
            ),
        ])
        harness.submit(identity_missing_review())
        current = doc("DOC-CURRENT", "correct-rear-plate.jpg", "license_plate_photo")
        extraction = DocumentExtractionResult(
            usable=True,
            reason="Rear damage and vehicle identity are readable.",
            supported_capabilities=[
                "damage_evidence", "license_plate_photo", "vehicle_identity"
            ],
            evidence_findings=["The identified grey sedan has rear damage."],
        )

        with self.assertRaisesRegex(ClaimReviewError, "429"):
            harness.deliver(
                current,
                extraction,
                auto_reconciliation_review(),
                ClaimReviewError("429 RESOURCE_EXHAUSTED"),
            )
        self.assertEqual(harness.repository.supersession_writes, 0)
        document_count = len(harness.repository.documents)

        result = harness.deliver(
            current,
            extraction,
            auto_reconciliation_review(),
            complete_review(),
        )

        self.assertEqual(result.final_status, "inspection_ready")
        self.assertEqual(len(harness.repository.documents), document_count)
        self.assertEqual(harness.repository.supersession_writes, 1)
        self.assertEqual(harness.repository.action_consumptions, 1)
        replay = harness.resume.resume(CLAIM_ID, current)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(harness.repository.supersession_writes, 1)

    def test_flows_2_to_4_retryable_review_matrix_is_same_operation_safe(self):
        for flow in (2, 3, 4):
            with self.subTest(flow=flow):
                old_id = "DOC-OLD" if flow in {3, 4} else None
                docs = [doc("DOC-REPORT", "police-report.pdf", "police_report")]
                if old_id:
                    docs.append(doc(old_id, "old.jpg", "damage_evidence"))
                harness = WorkflowMatrixHarness(docs)
                action = (
                    UploadDocumentRequestedAction(
                        action_id=f"ACT-FLOW-{flow}", review_id=f"AUTO-{flow}",
                        document_type="damage_evidence", instruction="Replace it.",
                        replaces_document_id=old_id,
                    ) if old_id else None
                )
                harness.submit(identity_missing_review(action=action))
                new = doc(f"DOC-NEW-{flow}", "correct.jpg", "license_plate_photo")
                harness.repository.documents[new.document_id] = new
                harness.extractor.extract.return_value = DocumentExtractionResult(
                    usable=True, reason="Current evidence is readable.",
                    supported_capabilities=["damage_evidence", "license_plate_photo", "vehicle_identity"],
                )
                harness.review.queue(
                    ClaimReviewError("429 RESOURCE_EXHAUSTED"), complete_review()
                )
                with self.assertRaisesRegex(ClaimReviewError, "429"):
                    harness.resume.resume(CLAIM_ID, new)
                document_count = len(harness.repository.documents)
                first_attempt_event_ids = set(harness.repository.events)
                result = harness.resume.resume(CLAIM_ID, new)
                self.assertEqual(result.final_status, "inspection_ready")
                self.assertEqual(len(harness.repository.documents), document_count)
                self.assertTrue(
                    first_attempt_event_ids.issubset(harness.repository.events)
                )
                self.assertEqual(
                    len(harness.repository.events),
                    len(first_attempt_event_ids) + 1,
                )
                self.assertEqual(harness.repository.create_document_calls, 0)
                self.assertEqual(harness.repository.supersession_writes, 1 if old_id else 0)
                self.assertEqual(harness.repository.action_consumptions, 1 if old_id else 0)
                self.assertEqual(harness.snapshot()["decision_generations"], 1)

    def test_unusable_replacement_keeps_original_active_and_action_retryable(self):
        action = UploadDocumentRequestedAction(
            action_id="ACT-RETRY-REPLACEMENT", review_id="AUTONOMOUS-RETRY",
            document_type="damage_evidence", instruction="Upload a usable replacement.",
            replaces_document_id="DOC-OLD",
        )
        harness = WorkflowMatrixHarness([
            doc("DOC-REPORT", "police-report.pdf", "police_report"),
            doc("DOC-OLD", "old-front.jpg", "damage_evidence"),
        ])
        harness.submit(identity_missing_review(action=action))
        unusable = doc("DOC-BLURRY", "blurry.jpg", "damage_evidence")
        result = harness.deliver(
            unusable,
            DocumentExtractionResult(
                usable=False,
                reason="The replacement is too blurry to establish current evidence.",
            ),
            identity_missing_review(action=action),
        )

        self.assertEqual(result.final_status, "awaiting_documents")
        snapshot = harness.snapshot()
        self.assertIn("DOC-OLD", snapshot["active_document_ids"])
        self.assertEqual(snapshot["superseded_document_ids"], [])
        self.assertEqual(snapshot["requested_actions"], ["ACT-RETRY-REPLACEMENT"])
        self.assertEqual(harness.repository.supersession_writes, 0)
        self.assertEqual(harness.repository.action_consumptions, 0)

    def test_request_more_info_rechecks_and_creates_new_ready_generation(self):
        harness = WorkflowMatrixHarness([
            doc("DOC-REPORT", "police-report.pdf", "police_report"),
            doc("DOC-REAR", "rear-plate.jpg", "damage_evidence",
                capabilities=("damage_evidence", "license_plate_photo", "vehicle_identity")),
        ])
        harness.submit(complete_review())
        self.assertEqual(harness.snapshot()["decision_generations"], 1)

        action = UploadDocumentRequestedAction(
            action_id="ACT-MORE-INFO", review_id="HRV-MATRIX-1",
            document_type="damage_evidence",
            instruction="Please upload a clearer passenger-side rear damage photo.",
        )
        harness.repository.claim.update({
            "status": "awaiting_documents",
            "requested_actions": [action.model_dump(mode="python")],
            "intake_complete": False,
        })
        additional = doc("DOC-MORE-INFO", "rear-detail.jpg", "damage_evidence")
        additional = additional.model_copy(
            update={"requested_action_id": action.action_id}
        )
        result = harness.deliver(
            additional,
            DocumentExtractionResult(
                usable=True, reason="The requested rear detail is clear.",
                supported_capabilities=["damage_evidence"],
            ),
            complete_review(),
        )

        self.assertEqual(result.final_status, "inspection_ready")
        snapshot = harness.snapshot()
        self.assertEqual(snapshot["requested_actions"], [])
        self.assertEqual(snapshot["decision_generations"], 2)
        self.assertEqual(harness.repository.supersession_writes, 0)

    def test_equivalent_current_evidence_is_order_independent(self):
        def run(documents):
            harness = WorkflowMatrixHarness(documents)
            harness.submit(complete_review())
            return harness.snapshot(), harness.review.active_sources[-1]

        report = doc("DOC-REPORT", "police-report.pdf", "police_report")
        rear = doc(
            "DOC-REAR", "rear-plate.jpg", "damage_evidence",
            capabilities=("damage_evidence", "license_plate_photo", "vehicle_identity"),
        )
        first = run([report, rear])
        second = run([rear, report])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
