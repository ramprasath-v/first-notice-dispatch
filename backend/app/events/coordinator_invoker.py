from typing import Any
from uuid import uuid4

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.firstnotice_adk import FirstNoticeCoordinatorAgent


class AdkClaimCoordinatorInvoker:
    """Run ADK until review reaches the next event-driven durable boundary."""

    def __init__(self, coordinator: FirstNoticeCoordinatorAgent) -> None:
        self._coordinator = coordinator

    async def process_submitted_claim(self, claim_id: str) -> dict[str, Any]:
        session_service = InMemorySessionService()
        session_id = f"pubsub-{uuid4()}"
        await session_service.create_session(
            app_name="agents",
            user_id="pubsub-handler",
            session_id=session_id,
            state={"claim_id": claim_id, "stop_after_review": True},
        )
        runner = Runner(
            app_name="agents",
            agent=self._coordinator,
            session_service=session_service,
        )
        stream = runner.run_async(
            user_id="pubsub-handler",
            session_id=session_id,
            new_message=types.Content(
                role="user",
                parts=[types.Part.from_text(text="Process the submitted claim event.")],
            ),
        )
        last_output: dict[str, Any] = {}
        review_output: dict[str, Any] = {}
        async for adk_event in stream:
            if not isinstance(adk_event.output, dict):
                continue
            last_output = adk_event.output
            if adk_event.output.get("kind") == "review_completed":
                review_output = adk_event.output
        return review_output or last_output
