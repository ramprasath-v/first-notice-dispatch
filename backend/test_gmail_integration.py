import argparse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from app.integrations.gmail_service import (
    AdjusterEmailRequest,
    GmailService,
    GmailSettings,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one real FirstNotice Gmail integration test message."
    )
    parser.add_argument(
        "--confirm-send",
        action="store_true",
        help="Required acknowledgement that one real email will be sent.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    if not args.confirm_send:
        raise RuntimeError(
            "No email sent. Re-run with --confirm-send to send one real test message."
        )
    settings = GmailSettings.from_env()
    if not settings.enabled or not settings.adjuster_email or not settings.sender_email:
        raise RuntimeError("Gmail integration must be fully enabled for the live test.")
    now = datetime.now(timezone.utc)
    result = GmailService.from_oauth_settings(settings).send_adjuster_email(
        AdjusterEmailRequest(
            notification_id=f"NTF-DEMO-{now.strftime('%Y%m%d%H%M%S')}",
            claim_id="CLM-DEMO-TEST",
            recipient=settings.adjuster_email,
            sender=settings.sender_email,
            subject="[FirstNotice Demo Test] Gmail Integration",
            adjuster_summary="This is a one-shot Gmail integration diagnostic.",
            incident_summary="Synthetic integration test; no real claim data.",
            intake_priority="routine",
            inspection_start=now + timedelta(days=1),
            inspection_end=now + timedelta(days=1, hours=1),
            inspection_location="Demo only",
            inspection_type="virtual",
            evidence_summary=["Synthetic test data only"],
            unresolved_notes=[],
            action_requested="No action required; verify receipt, then delete.",
        )
    )
    print("success")
    print(f"recipient: {result.recipient}")
    print(f"gmail_message_id: {result.gmail_message_id}")


if __name__ == "__main__":
    main()
