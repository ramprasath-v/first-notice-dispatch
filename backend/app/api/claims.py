from collections.abc import Callable

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile, status

from app.models.claim_api import (
    ClaimAcceptedResponse,
    ClaimSummaryResponse,
    ClaimTimelineEvent,
    DocumentAcceptedResponse,
)
from app.services.claim_storage_service import ClaimStorageValidationError
from app.services.claim_submission_service import (
    ClaimNotFoundError,
    ClaimSubmissionError,
    ClaimSubmissionService,
    EvidenceUpload,
)


def create_claims_router(
    get_service: Callable[[], ClaimSubmissionService],
) -> APIRouter:
    router = APIRouter(prefix="/claims", tags=["claims"])

    @router.post(
        "",
        response_model=ClaimAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def submit_claim(
        idempotency_key: str = Header(
            ..., alias="X-Idempotency-Key", min_length=8, max_length=128
        ),
        incident_description: str = Form(..., min_length=1, max_length=4000),
        policy_number_hint: str | None = Form(default=None, max_length=128),
        files: list[UploadFile] = File(...),
    ) -> ClaimAcceptedResponse:
        try:
            return get_service().submit_claim(
                incident_description=incident_description,
                policy_number_hint=policy_number_hint,
                evidence=[_evidence_upload(file) for file in files],
                idempotency_key=idempotency_key,
            )
        except ClaimStorageValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ClaimSubmissionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/{claim_id}", response_model=ClaimSummaryResponse)
    def get_claim(claim_id: str) -> ClaimSummaryResponse:
        try:
            return get_service().get_claim(claim_id)
        except ClaimNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get(
        "/{claim_id}/events",
        response_model=list[ClaimTimelineEvent],
    )
    def get_claim_events(claim_id: str) -> list[ClaimTimelineEvent]:
        try:
            return get_service().get_timeline(claim_id)
        except ClaimNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post(
        "/{claim_id}/documents",
        response_model=DocumentAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def add_claim_document(
        claim_id: str,
        document_type: str = Form(..., min_length=1, max_length=64),
        file: UploadFile = File(...),
    ) -> DocumentAcceptedResponse:
        try:
            return get_service().add_missing_document(
                claim_id=claim_id,
                document_type=document_type,
                evidence=_evidence_upload(file),
            )
        except ClaimNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ClaimStorageValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ClaimSubmissionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return router


def _evidence_upload(file: UploadFile) -> EvidenceUpload:
    return EvidenceUpload(
        file_obj=file.file,
        filename=file.filename,
        content_type=file.content_type,
    )
