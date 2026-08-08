import argparse
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv

from app.config import Settings
from app.events.claim_events import (
    ClaimDocumentReceivedEvent,
    ClaimEvent,
    ClaimInspectionReadyEvent,
    ClaimSubmittedEvent,
    DocumentReceivedPayload,
)
from app.events.pubsub_publisher import ClaimEventPublisher, PubSubSettings
from app.events.runtime import build_claim_event_handler


EVENT_TYPES = (
    "claim.submitted",
    "claim.document.received",
    "claim.inspection.ready",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish or locally handle a claim event.")
    parser.add_argument("--event", required=True, choices=EVENT_TYPES)
    parser.add_argument("--claim-id", required=True)
    parser.add_argument("--document-id")
    parser.add_argument("--event-id")
    parser.add_argument("--correlation-id")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Call the handler directly without publishing to Pub/Sub.",
    )
    return parser.parse_args()


def build_event(args: argparse.Namespace) -> ClaimEvent:
    common = {
        "claim_id": args.claim_id,
        "occurred_at": datetime.now(timezone.utc),
        "source": "local-event-demo",
    }
    if args.event_id:
        common["event_id"] = args.event_id
    if args.correlation_id:
        common["correlation_id"] = args.correlation_id

    if args.event == "claim.submitted":
        return ClaimSubmittedEvent(event_type=args.event, **common)
    if args.event == "claim.document.received":
        if not args.document_id:
            raise ValueError("--document-id is required for claim.document.received.")
        return ClaimDocumentReceivedEvent(
            event_type=args.event,
            payload=DocumentReceivedPayload(document_id=args.document_id),
            **common,
        )
    return ClaimInspectionReadyEvent(event_type=args.event, **common)


async def run_local(event: ClaimEvent) -> None:
    handler = build_claim_event_handler(Settings.from_env())
    result = await handler.handle(event)
    print(result.model_dump_json(indent=2))


def main() -> None:
    load_dotenv()
    args = parse_args()
    event = build_event(args)
    if args.local:
        asyncio.run(run_local(event))
        return
    message_id = ClaimEventPublisher(PubSubSettings.from_env()).publish(event)
    print(f"Published {event.event_type} event {event.event_id}")
    print(f"Pub/Sub message ID: {message_id}")


if __name__ == "__main__":
    main()
