from collections.abc import Callable

from fastapi import APIRouter, Header, HTTPException, status

from app.models.human_review import (
    ClaimCorrectionAcceptedResponse,
    ClaimCorrectionRequest,
    HumanReviewDecisionRequest,
    HumanReviewDecisionResponse,
    HumanReviewPublicResponse,
)
from app.services.human_review_service import (
    HumanReviewConflictError,
    HumanReviewExpiredError,
    HumanReviewNotFoundError,
    HumanReviewService,
)


def create_reviews_router(
    get_service: Callable[[], HumanReviewService],
) -> APIRouter:
    router = APIRouter(tags=["human-review"])

    @router.get("/reviews/current", response_model=HumanReviewPublicResponse)
    def get_review(
        token: str = Header(..., alias="X-Review-Token", min_length=16, max_length=256)
    ) -> HumanReviewPublicResponse:
        return _call(lambda: get_service().get_public_review(token))

    @router.post(
        "/reviews/current/approve",
        response_model=HumanReviewDecisionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def approve_review(
        request: HumanReviewDecisionRequest,
        token: str = Header(..., alias="X-Review-Token", min_length=16, max_length=256),
    ) -> HumanReviewDecisionResponse:
        return _call(lambda: get_service().approve(token, request))

    @router.post(
        "/reviews/current/request-correction",
        response_model=HumanReviewDecisionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def request_correction(
        request: HumanReviewDecisionRequest,
        token: str = Header(..., alias="X-Review-Token", min_length=16, max_length=256),
    ) -> HumanReviewDecisionResponse:
        return _call(lambda: get_service().request_correction(token, request))

    @router.post(
        "/claims/{claim_id}/corrections",
        response_model=ClaimCorrectionAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def submit_correction(
        claim_id: str, request: ClaimCorrectionRequest
    ) -> ClaimCorrectionAcceptedResponse:
        return _call(
            lambda: get_service().submit_correction(
                claim_id, field_name=request.field_name, value=request.value
            )
        )

    return router


def _call(operation):
    try:
        return operation()
    except HumanReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HumanReviewExpiredError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except HumanReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="The review service is temporarily unavailable."
        ) from exc
