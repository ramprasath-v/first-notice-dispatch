from collections.abc import AsyncGenerator
from enum import StrEnum
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import Field

from app.models.adk_orchestration import (
    CoordinatorResult,
    EvidenceInput,
)
from app.services.intake_extraction_service import IntakeExtractionService
from app.tools.adk_workflow_tools import ClaimWorkflowToolAdapter


class CoordinatorAction(StrEnum):
    RUN_INTAKE = "run_intake"
    RUN_REVIEW = "run_review"
    WAIT_FOR_EVIDENCE = "wait_for_external_evidence"
    DISPATCH_CLAIM = "dispatch_claim"
    WAIT_FOR_INSPECTION_DECISION = "wait_for_inspection_decision"
    STOP_FOR_HUMAN = "stop_for_human_review"
    COMPLETE = "workflow_complete"


class UnsupportedCoordinatorState(ValueError):
    """Raised when durable claim state has no automatic ADK route."""


def route_claim_state(status: str) -> CoordinatorAction:
    routes = {
        "new": CoordinatorAction.RUN_INTAKE,
        "intake_complete": CoordinatorAction.RUN_REVIEW,
        "review_processing": CoordinatorAction.RUN_REVIEW,
        "awaiting_documents": CoordinatorAction.WAIT_FOR_EVIDENCE,
        "inspection_ready": CoordinatorAction.WAIT_FOR_INSPECTION_DECISION,
        "inspection_pending": CoordinatorAction.DISPATCH_CLAIM,
        "inspection_scheduled": CoordinatorAction.DISPATCH_CLAIM,
        "human_review_required": CoordinatorAction.STOP_FOR_HUMAN,
        "adjuster_notified": CoordinatorAction.COMPLETE,
    }
    try:
        return routes[status]
    except KeyError as exc:
        raise UnsupportedCoordinatorState(
            f"FirstNoticeCoordinator cannot route claim state {status!r}."
        ) from exc


class IntakeSpecialistAgent(BaseAgent):
    extraction_service: Any = Field(exclude=True)
    workflow_tools: Any = Field(exclude=True)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        evidence_inputs = [
            EvidenceInput.model_validate(item)
            for item in ctx.session.state.get("evidence_inputs", [])
        ]
        claim_id = ctx.session.state.get("claim_id")
        if not evidence_inputs and claim_id:
            evidence_inputs = self.workflow_tools.get_claim_evidence_inputs(
                str(claim_id)
            )
        if not evidence_inputs:
            raise ValueError("Intake Agent requires evidence_inputs in ADK state.")

        unique_paths = list(dict.fromkeys(item.path for item in evidence_inputs))
        intake_context = (
            self.workflow_tools.get_claim_intake_context(str(claim_id))
            if claim_id
            else {}
        )
        if any(intake_context.values()):
            intake_result = self.extraction_service.extract(
                unique_paths,
                incident_description=intake_context.get("incident_description"),
                policy_number_hint=intake_context.get("policy_number_hint"),
            )
        else:
            intake_result = self.extraction_service.extract(unique_paths)
        if claim_id:
            claim_state = self.workflow_tools.complete_claim_intake(
                str(claim_id), intake_result
            )
        else:
            claim_state = self.workflow_tools.create_claim_record(
                intake_result, evidence_inputs
            )
        ctx.session.state["claim_id"] = claim_state.claim_id
        yield _agent_event(
            author=self.name,
            message=f"Intake completed for {claim_state.claim_id}.",
            output={
                "kind": "intake_completed",
                "claim_state": claim_state.model_dump(mode="python"),
                "intake_result": intake_result.model_dump(mode="python"),
            },
            state_delta={"claim_id": claim_state.claim_id},
        )


class ReviewSpecialistAgent(BaseAgent):
    workflow_tools: Any = Field(exclude=True)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        claim_id = ctx.session.state.get("claim_id")
        if not claim_id:
            raise ValueError("Review Agent requires claim_id in ADK state.")
        claim_state = self.workflow_tools.run_claim_review(str(claim_id))
        yield _agent_event(
            author=self.name,
            message=(
                f"Review completed for {claim_state.claim_id}: "
                f"{claim_state.status}."
            ),
            output={
                "kind": "review_completed",
                "claim_state": claim_state.model_dump(mode="python"),
            },
        )


class FirstNoticeCoordinatorAgent(BaseAgent):
    workflow_tools: Any = Field(exclude=True)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        claim_id = ctx.session.state.get("claim_id")
        is_new_claim = bool(ctx.session.state.get("new_claim", False))
        if not claim_id and not is_new_claim:
            raise ValueError(
                "FirstNoticeCoordinator requires claim_id or new_claim=true."
            )

        current_status = "new" if is_new_claim and not claim_id else ""
        initial_status = current_status
        selected_actions: list[str] = []

        for _ in range(6):
            if claim_id:
                durable_state = self.workflow_tools.get_claim_state(str(claim_id))
                current_status = durable_state.status
                if not initial_status:
                    initial_status = current_status

            action = route_claim_state(current_status)
            selected_actions.append(action.value)
            yield _agent_event(
                author=self.name,
                message=f"Selected action: {action.value} for state {current_status}.",
                output={
                    "kind": "coordinator_action",
                    "action": action.value,
                    "status": current_status,
                },
            )

            if action == CoordinatorAction.RUN_INTAKE:
                intake_agent = self._sub_agent("FirstNoticeIntakeAgent")
                async for event in intake_agent.run_async(ctx):
                    if isinstance(event.output, dict):
                        claim_data = event.output.get("claim_state")
                        if isinstance(claim_data, dict):
                            claim_id = claim_data.get("claim_id")
                            if claim_id:
                                ctx.session.state["claim_id"] = claim_id
                    yield event
                current_status = "intake_complete"
                continue

            if action == CoordinatorAction.RUN_REVIEW:
                review_agent = self._sub_agent("FirstNoticeReviewAgent")
                async for event in review_agent.run_async(ctx):
                    yield event
                if ctx.session.state.get("stop_after_review", False):
                    if not claim_id:
                        raise ValueError("Review boundary requires a persisted claim_id.")
                    reviewed_state = self.workflow_tools.get_claim_state(str(claim_id))
                    if reviewed_state.status in {
                        "awaiting_documents",
                        "inspection_ready",
                        "inspection_pending",
                        "human_review_required",
                    }:
                        yield _final_event(
                            self.name,
                            CoordinatorResult(
                                claim_id=str(claim_id),
                                initial_status=initial_status,
                                final_status=reviewed_state.status,
                                selected_actions=selected_actions,
                                stop_reason="event_boundary_reached",
                            ),
                        )
                        return
                continue

            if action == CoordinatorAction.DISPATCH_CLAIM:
                if not claim_id:
                    raise ValueError("Dispatch requires a persisted claim_id.")
                self.workflow_tools.dispatch_to_adjuster(str(claim_id))
                continue

            if action == CoordinatorAction.WAIT_FOR_EVIDENCE:
                return_event = _final_event(
                    self.name,
                    CoordinatorResult(
                        claim_id=str(claim_id),
                        initial_status=initial_status,
                        final_status=current_status,
                        selected_actions=selected_actions,
                        stop_reason="awaiting_external_evidence",
                    ),
                )
                yield return_event
                return

            if action == CoordinatorAction.STOP_FOR_HUMAN:
                yield _final_event(
                    self.name,
                    CoordinatorResult(
                        claim_id=str(claim_id),
                        initial_status=initial_status,
                        final_status=current_status,
                        selected_actions=selected_actions,
                        stop_reason="human_review_required",
                    ),
                )
                return

            if action == CoordinatorAction.WAIT_FOR_INSPECTION_DECISION:
                yield _final_event(
                    self.name,
                    CoordinatorResult(
                        claim_id=str(claim_id),
                        initial_status=initial_status,
                        final_status=current_status,
                        selected_actions=selected_actions,
                        stop_reason="awaiting_inspection_decision",
                    ),
                )
                return

            if action == CoordinatorAction.COMPLETE:
                yield _final_event(
                    self.name,
                    CoordinatorResult(
                        claim_id=str(claim_id),
                        initial_status=initial_status,
                        final_status=current_status,
                        selected_actions=selected_actions,
                        stop_reason="workflow_complete",
                    ),
                )
                return

        raise RuntimeError("FirstNoticeCoordinator exceeded its deterministic step limit.")

    def _sub_agent(self, name: str) -> BaseAgent:
        for agent in self.sub_agents:
            if agent.name == name:
                return agent
        raise RuntimeError(f"Required ADK sub-agent {name} is not configured.")


def build_firstnotice_coordinator(
    *,
    extraction_service: IntakeExtractionService,
    workflow_tools: ClaimWorkflowToolAdapter,
) -> FirstNoticeCoordinatorAgent:
    intake_agent = IntakeSpecialistAgent(
        name="FirstNoticeIntakeAgent",
        description="Runs the existing structured multimodal intake service.",
        extraction_service=extraction_service,
        workflow_tools=workflow_tools,
    )
    review_agent = ReviewSpecialistAgent(
        name="FirstNoticeReviewAgent",
        description="Runs deterministic gap-check and existing review service.",
        workflow_tools=workflow_tools,
    )
    return FirstNoticeCoordinatorAgent(
        name="FirstNoticeCoordinator",
        description="Deterministically coordinates the FirstNotice workflow.",
        workflow_tools=workflow_tools,
        sub_agents=[intake_agent, review_agent],
    )


def _agent_event(
    *,
    author: str,
    message: str,
    output: dict[str, Any],
    state_delta: dict[str, Any] | None = None,
) -> Event:
    return Event(
        author=author,
        content=types.Content(
            role="model", parts=[types.Part.from_text(text=message)]
        ),
        output=output,
        actions=EventActions(stateDelta=state_delta or {}),
    )


def _final_event(author: str, result: CoordinatorResult) -> Event:
    return _agent_event(
        author=author,
        message=(
            f"Workflow stopped at {result.final_status}: {result.stop_reason}."
        ),
        output={
            "kind": "coordinator_result",
            "result": result.model_dump(mode="python"),
        },
    )
