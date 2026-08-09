import base64
import binascii
import logging
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.claims import create_claims_router
from app.api.cors import configure_cors
from app.api.runtime import ApiDependencies, build_api_dependencies
from app.api.reviews import create_reviews_router
from app.events.claim_event_handler import (
    ClaimEventHandler,
    ClaimEventProcessingError,
)
from app.events.claim_events import ClaimEvent, parse_claim_event_json
from app.services.claim_submission_service import ClaimSubmissionService
from app.services.human_review_service import HumanReviewService


logger = logging.getLogger(__name__)


class PubSubPushMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    data: str = Field(min_length=1)
    message_id: str | None = Field(default=None, alias="messageId")


class PubSubPushEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: PubSubPushMessage
    subscription: str | None = None


def decode_push_envelope(envelope: PubSubPushEnvelope) -> ClaimEvent:
    try:
        decoded = base64.b64decode(envelope.message.data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Pub/Sub message.data is not valid base64.") from exc
    try:
        return parse_claim_event_json(decoded)
    except ValidationError as exc:
        raise ValueError("Pub/Sub message.data is not a valid ClaimEvent.") from exc


@lru_cache(maxsize=1)
def _default_dependencies() -> ApiDependencies:
    return build_api_dependencies()


def create_app(
    handler: ClaimEventHandler | None = None,
    claim_submission_service: ClaimSubmissionService | None = None,
    allowed_origins: list[str] | None = None,
    human_review_service: HumanReviewService | None = None,
) -> FastAPI:
    app = FastAPI(title="FirstNotice Dispatch API", version="1.0.0")

    @app.exception_handler(RequestValidationError)
    async def log_request_validation(
        request: Request, exc: RequestValidationError
    ):
        if request.url.path == "/events/pubsub":
            logger.warning(
                "Pub/Sub request validation failed before event handling.",
                extra={
                    "workflow_stage": "request_validation",
                    "request_path": request.url.path,
                    "validation_errors": [
                        {
                            "location": ".".join(str(item) for item in error["loc"]),
                            "type": error["type"],
                        }
                        for error in exc.errors()
                    ],
                },
            )
        return await request_validation_exception_handler(request, exc)
    configure_cors(app, allowed_origins)
    app.include_router(
        create_claims_router(
            lambda: claim_submission_service
            if claim_submission_service is not None
            else _default_dependencies().claim_submission_service
        )
    )
    app.include_router(
        create_reviews_router(
            lambda: human_review_service
            if human_review_service is not None
            else _default_dependencies().human_review_service
        )
    )

    @app.post("/events/pubsub")
    async def receive_pubsub_event(envelope: PubSubPushEnvelope) -> dict[str, object]:
        try:
            event = decode_push_envelope(envelope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        active_handler = (
            handler
            if handler is not None
            else _default_dependencies().event_handler
        )
        try:
            result = await active_handler.handle(event)
        except ClaimEventProcessingError as exc:
            underlying = exc.__cause__ or exc
            safe_traceback_exception = RuntimeError(str(exc))
            logger.error(
                "Pub/Sub claim event processing failed.",
                extra={
                    "exception_type": type(underlying).__name__,
                    "exception_message": str(exc),
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "claim_id": event.claim_id,
                    "pubsub_message_id": envelope.message.message_id,
                    "workflow_stage": exc.stage,
                },
                exc_info=(
                    RuntimeError,
                    safe_traceback_exception,
                    underlying.__traceback__,
                ),
            )
            status_code = 503 if exc.retryable else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return result.model_dump(mode="json")

    return app

app = create_app()
