from app.agents.runtime import build_adk_runtime
from app.config import Settings
from app.events.claim_event_handler import ClaimEventHandler
from app.events.pubsub_publisher import ClaimEventPublisher, PubSubSettings
from app.events.coordinator_invoker import AdkClaimCoordinatorInvoker
from app.integrations.gmail_service import GmailService, GmailSettings
from app.services.human_review_service import (
    HumanReviewResumeWorkflow,
    HumanReviewService,
    HumanReviewSettings,
)
from app.tools.firestore_repository import FirestoreClaimRepository


def build_claim_event_handler(
    settings: Settings,
    *,
    repository: FirestoreClaimRepository | None = None,
    publisher: ClaimEventPublisher | None = None,
    human_review_service: HumanReviewService | None = None,
) -> ClaimEventHandler:
    runtime = build_adk_runtime(settings, repository=repository)
    publisher = publisher or ClaimEventPublisher(PubSubSettings.from_env())
    if human_review_service is None:
        gmail_settings = GmailSettings.from_env()
        human_review_service = HumanReviewService(
            repository=runtime.repository,
            publisher=publisher,
            settings=HumanReviewSettings.from_env(),
            gmail_sender=(
                GmailService.from_oauth_settings(gmail_settings)
                if gmail_settings.enabled
                else None
            ),
            recipient=gmail_settings.adjuster_email or "",
            sender=gmail_settings.sender_email or "",
        )
    return ClaimEventHandler(
        repository=runtime.repository,
        coordinator=AdkClaimCoordinatorInvoker(runtime.coordinator),
        resume_workflow=runtime.tools.resume_workflow,
        dispatch_workflow=runtime.tools.dispatch_workflow,
        publisher=publisher,
        human_review_service=human_review_service,
        human_review_resume_workflow=HumanReviewResumeWorkflow(runtime.repository),
    )
