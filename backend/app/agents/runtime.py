from dataclasses import dataclass

from app.agents.firstnotice_adk import (
    FirstNoticeCoordinatorAgent,
    build_firstnotice_coordinator,
)
from app.config import Settings
from app.integrations.google_calendar_service import (
    GoogleCalendarService,
    GoogleCalendarSettings,
)
from app.integrations.gmail_service import GmailService, GmailSettings
from app.services.adjuster_dispatch_service import AdjusterDispatchService
from app.services.claim_review_service import ClaimReviewService
from app.services.document_extraction_service import GeminiDocumentExtractor
from app.services.inspection_scheduling_service import InspectionSchedulingService
from app.services.intake_extraction_service import IntakeExtractionService
from app.tools.adk_workflow_tools import ClaimWorkflowToolAdapter
from app.tools.firestore_repository import FirestoreClaimRepository
from app.tools.gemini_client import create_gemini_client
from app.tools.notification_tools import (
    GmailAdjusterNotificationTool,
    MockAdjusterNotificationTool,
)
from app.workflows.claim_dispatch_workflow import ClaimDispatchWorkflow
from app.workflows.claim_resume_workflow import ClaimResumeWorkflow


@dataclass(frozen=True)
class FirstNoticeAdkRuntime:
    coordinator: FirstNoticeCoordinatorAgent
    tools: ClaimWorkflowToolAdapter
    repository: FirestoreClaimRepository


def build_adk_runtime(
    settings: Settings,
    *,
    repository: FirestoreClaimRepository | None = None,
) -> FirstNoticeAdkRuntime:
    client = create_gemini_client(settings)
    repository = repository or FirestoreClaimRepository.from_default_credentials(
        settings.google_cloud_project, settings.firestore_database
    )
    review_service = ClaimReviewService(client, settings.gemini_model)
    resume_workflow = ClaimResumeWorkflow(
        repository=repository,
        review_service=review_service,
        document_extractor=GeminiDocumentExtractor(
            client, settings.gemini_model
        ),
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
    dispatch_workflow = ClaimDispatchWorkflow(
        repository=repository,
        scheduling_service=InspectionSchedulingService(),
        adjuster_service=AdjusterDispatchService(client, settings.gemini_model),
        notification_tool=notification_tool,
        calendar_service=calendar_service,
    )
    tools = ClaimWorkflowToolAdapter(
        repository=repository,
        review_service=review_service,
        resume_workflow=resume_workflow,
        dispatch_workflow=dispatch_workflow,
    )
    coordinator = build_firstnotice_coordinator(
        extraction_service=IntakeExtractionService(client, settings.gemini_model),
        workflow_tools=tools,
    )
    return FirstNoticeAdkRuntime(
        coordinator=coordinator,
        tools=tools,
        repository=repository,
    )
