import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.runtime import build_adk_runtime
from app.config import Settings
from app.models.adk_orchestration import CoordinatorResult, EvidenceInput


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DATA_DIR = PROJECT_ROOT / "sample-data"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the FirstNotice workflow through Google ADK."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--claim-id")
    target.add_argument("--new-claim", action="store_true")
    parser.add_argument(
        "--missing-license-plate",
        action="store_true",
        help="For --new-claim, demonstrate a clean awaiting_documents stop.",
    )
    parser.add_argument(
        "--image", type=Path, default=SAMPLE_DATA_DIR / "accident-photo.jpg"
    )
    parser.add_argument(
        "--police-report", type=Path, default=SAMPLE_DATA_DIR / "police-report.pdf"
    )
    parser.add_argument("--audio", type=Path, default=None)
    return parser.parse_args()


async def run_demo(args: argparse.Namespace) -> CoordinatorResult:
    settings = Settings.from_env()
    runtime = build_adk_runtime(settings)
    session_service = InMemorySessionService()
    session_id = f"firstnotice-{uuid4()}"
    state: dict[str, object] = {}

    if args.claim_id:
        state["claim_id"] = args.claim_id
    else:
        image_path = args.image.expanduser().resolve()
        report_path = args.police_report.expanduser().resolve()
        evidence = [
            EvidenceInput(path=str(image_path), document_type="damage_evidence"),
            EvidenceInput(path=str(report_path), document_type="police_report"),
        ]
        if not args.missing_license_plate:
            evidence.append(
                EvidenceInput(
                    path=str(image_path), document_type="license_plate_photo"
                )
            )
        if args.audio:
            evidence.append(
                EvidenceInput(
                    path=str(args.audio.expanduser().resolve()),
                    document_type="voice_note",
                )
            )
        state.update(
            {
                "new_claim": True,
                "evidence_inputs": [
                    item.model_dump(mode="python") for item in evidence
                ],
            }
        )

    await session_service.create_session(
        app_name="agents",
        user_id="local-demo",
        session_id=session_id,
        state=state,
    )
    runner = Runner(
        app_name="agents",
        agent=runtime.coordinator,
        session_service=session_service,
    )

    final_result: CoordinatorResult | None = None
    print("FirstNoticeCoordinator")
    if args.claim_id:
        print(f"Claim: {args.claim_id}")
    else:
        print("Claim: new")

    async for event in runner.run_async(
        user_id="local-demo",
        session_id=session_id,
        new_message=types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text="Process this claim automatically until a stop state."
                )
            ],
        ),
    ):
        if not isinstance(event.output, dict):
            continue
        if event.output.get("kind") == "coordinator_action":
            print(f"State: {event.output['status']}")
            print(f"Selected action: {event.output['action']}")
        elif event.output.get("kind") == "intake_completed":
            claim_state = event.output["claim_state"]
            print(f"Created claim: {claim_state['claim_id']}")
        elif event.output.get("kind") == "review_completed":
            claim_state = event.output["claim_state"]
            print(f"Review status: {claim_state['status']}")
        elif event.output.get("kind") == "coordinator_result":
            final_result = CoordinatorResult.model_validate(event.output["result"])

    if final_result is None:
        raise RuntimeError("ADK coordinator completed without a final result.")
    print("\nFinal state:")
    print(final_result.final_status)
    print(f"Stop reason: {final_result.stop_reason}")
    return final_result


def main() -> None:
    load_dotenv()
    asyncio.run(run_demo(parse_args()))


if __name__ == "__main__":
    main()
