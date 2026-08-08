from functools import lru_cache

from fastapi import FastAPI

from app.api.claims import create_claims_router
from app.api.cors import configure_cors
from app.api.runtime import ApiDependencies, build_api_dependencies
from app.api.reviews import create_reviews_router
from app.services.claim_submission_service import ClaimSubmissionService
from app.services.human_review_service import HumanReviewService


@lru_cache(maxsize=1)
def _default_dependencies() -> ApiDependencies:
    return build_api_dependencies()


def create_claimant_app(
    claim_submission_service: ClaimSubmissionService | None = None,
    allowed_origins: list[str] | None = None,
    human_review_service: HumanReviewService | None = None,
) -> FastAPI:
    """Create the browser-facing app without the Pub/Sub receiver route."""
    app = FastAPI(
        title="FirstNotice Dispatch Claimant API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    configure_cors(app, allowed_origins)

    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    api.include_router(
        create_claims_router(
            lambda: claim_submission_service
            if claim_submission_service is not None
            else _default_dependencies().claim_submission_service
        )
    )
    api.include_router(
        create_reviews_router(
            lambda: human_review_service
            if human_review_service is not None
            else _default_dependencies().human_review_service
        )
    )
    app.mount("/api", api)
    return app


app = create_claimant_app()
