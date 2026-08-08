import argparse

from dotenv import load_dotenv

from app.config import Settings
from app.integrations.google_calendar_service import (
    GoogleCalendarService,
    GoogleCalendarSettings,
)
from app.integrations.gmail_service import GmailService, GmailSettings
from app.services.adjuster_dispatch_service import AdjusterDispatchService
from app.services.inspection_scheduling_service import InspectionSchedulingService
from app.tools.firestore_repository import FirestoreClaimRepository
from app.tools.gemini_client import create_gemini_client
from app.tools.notification_tools import (
    GmailAdjusterNotificationTool,
    MockAdjusterNotificationTool,
)
from app.workflows.claim_dispatch_workflow import ClaimDispatchWorkflow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Schedule inspection and dispatch a FirstNotice claim."
    )
    parser.add_argument("--claim-id", required=True)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    settings = Settings.from_env()
    client = create_gemini_client(settings)
    repository = FirestoreClaimRepository.from_default_credentials(
        settings.google_cloud_project,
        settings.firestore_database,
    )
    calendar_settings = GoogleCalendarSettings.from_env()
    calendar_service = (
        GoogleCalendarService.from_default_credentials(calendar_settings)
        if calendar_settings.enabled
        else None
    )
    gmail_settings = GmailSettings.from_env()
    notification_tool = (
        GmailAdjusterNotificationTool(
            repository,
            GmailService.from_oauth_settings(gmail_settings),
            recipient=gmail_settings.adjuster_email or "",
            sender=gmail_settings.sender_email or "",
        )
        if gmail_settings.enabled
        else MockAdjusterNotificationTool(repository)
    )
    workflow = ClaimDispatchWorkflow(
        repository=repository,
        scheduling_service=InspectionSchedulingService(),
        adjuster_service=AdjusterDispatchService(client, settings.gemini_model),
        notification_tool=notification_tool,
        calendar_service=calendar_service,
    )
    claim = repository.get_claim(args.claim_id)
    if claim is None:
        raise RuntimeError(f"Claim {args.claim_id} was not found.")

    print(f"Claim: {args.claim_id}")
    print(f"Current status: {claim.get('status')}")
    result = workflow.dispatch(args.claim_id)

    if result.candidate_slots:
        print("\nInspection slots:")
        for index, slot in enumerate(result.candidate_slots, start=1):
            print(f"{index}. {slot.scheduled_start.strftime('%Y-%m-%d %H:%M UTC')}")
    print("\nSelected:")
    print(result.appointment.scheduled_start.strftime("%Y-%m-%d %H:%M UTC"))
    print("\nAppointment:")
    print(result.appointment.appointment_id)
    if result.appointment.calendar_event_id:
        print("\nGoogle Calendar event:")
        print(result.appointment.calendar_event_id)
        if result.appointment.calendar_event_link:
            print(result.appointment.calendar_event_link)
    print("\nAdjuster packet created")
    print("\nAdjuster notification:")
    print(f"{result.notification.status} to {result.notification.recipient}")
    if result.notification.gmail_message_id:
        print(f"Gmail message ID: {result.notification.gmail_message_id}")
    if result.idempotent_replay:
        print("Idempotency: already dispatched; no duplicate work created")
    print("\nStatus:")
    if result.previous_status == "inspection_pending":
        print("inspection_pending")
        print("-> inspection_scheduled")
        print("-> adjuster_notified")
    else:
        print(f"{result.previous_status} -> {result.final_status}")


if __name__ == "__main__":
    main()
