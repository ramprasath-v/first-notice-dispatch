from dataclasses import dataclass

from app.config import Settings
from app.events.claim_event_handler import ClaimEventHandler
from app.events.pubsub_publisher import ClaimEventPublisher, PubSubSettings
from app.events.runtime import build_claim_event_handler
from app.integrations.gmail_service import GmailService, GmailSettings
from app.services.human_review_service import HumanReviewService, HumanReviewSettings
from app.services.claim_storage_service import ClaimStorageService, GcsSettings
from app.services.claim_submission_service import ClaimSubmissionService
from app.services.voice_incident_extraction_service import GeminiVoiceIncidentExtractor
from app.tools.gemini_client import create_gemini_client
from app.tools.firestore_repository import FirestoreClaimRepository


@dataclass(frozen=True)
class ApiDependencies:
    event_handler: ClaimEventHandler
    claim_submission_service: ClaimSubmissionService
    human_review_service: HumanReviewService


def build_api_dependencies() -> ApiDependencies:
    settings = Settings.from_env()
    repository = FirestoreClaimRepository.from_default_credentials(
        settings.google_cloud_project,
        settings.firestore_database,
    )
    storage_service = ClaimStorageService(GcsSettings.from_env())
    gemini_client = create_gemini_client(settings)
    publisher = ClaimEventPublisher(PubSubSettings.from_env())
    gmail_settings = GmailSettings.from_env()
    human_review_service = HumanReviewService(
        repository=repository,
        publisher=publisher,
        settings=HumanReviewSettings.from_env(),
        storage_service=storage_service,
        voice_incident_extractor=GeminiVoiceIncidentExtractor(
            gemini_client, settings.gemini_model
        ),
        gmail_sender=(
            GmailService.from_oauth_settings(gmail_settings)
            if gmail_settings.enabled
            else None
        ),
        recipient=gmail_settings.adjuster_email or "",
        sender=gmail_settings.sender_email or "",
    )
    event_handler = build_claim_event_handler(
        settings,
        repository=repository,
        publisher=publisher,
        human_review_service=human_review_service,
    )
    return ApiDependencies(
        event_handler=event_handler,
        claim_submission_service=ClaimSubmissionService(
            repository=repository,
            storage_service=storage_service,
            publisher=publisher,
        ),
        human_review_service=human_review_service,
    )
