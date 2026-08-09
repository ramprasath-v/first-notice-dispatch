from uuid import uuid4

from google.genai import types

from app.domain.claim_status import ClaimStatus
from app.models.claim_document import (
    ClaimDocument,
    DocumentExtractionResult,
    ResumeClaimResult,
)
from app.models.intake_result import intake_result_from_claim
from app.models.review_result import ClaimEvidenceMetadata, UploadedEvidence
from app.models.requested_action import (
    UploadDocumentRequestedAction,
    parse_requested_actions,
)
from app.services.claim_review_service import ClaimReviewService
from app.services.document_extraction_service import DocumentExtractor
from app.services.intake_extraction_service import evidence_part
from app.tools.firestore_repository import FirestoreClaimRepository


class ClaimResumeError(RuntimeError):
    """Raised when a missing-evidence workflow cannot safely resume."""


class ClaimResumeWorkflow:
    def __init__(
        self,
        repository: FirestoreClaimRepository,
        review_service: ClaimReviewService,
        document_extractor: DocumentExtractor,
    ) -> None:
        self._repository = repository
        self._review_service = review_service
        self._document_extractor = document_extractor

    def resume(
        self,
        claim_id: str,
        new_document: ClaimDocument,
        *,
        extraction_result: DocumentExtractionResult | None = None,
    ) -> ResumeClaimResult:
        if new_document.claim_id != claim_id:
            raise ClaimResumeError(
                "The new document claim_id does not match the requested claim."
            )

        claim = self._repository.get_claim(claim_id)
        if claim is None:
            raise ClaimResumeError(f"Claim {claim_id} does not exist.")

        idempotency_key = f"{claim_id}:{new_document.document_id}:resume"
        existing_document = self._repository.get_document(
            claim_id, new_document.document_id
        )
        if (
            existing_document is not None
            and existing_document.resume_idempotency_key == idempotency_key
            and existing_document.resume_processed_at is not None
        ):
            return ResumeClaimResult(
                claim_id=claim_id,
                document_id=new_document.document_id,
                previous_status=claim.get("status", "unknown"),
                final_status=(
                    existing_document.resume_result_status
                    or claim.get("status", "unknown")
                ),
                matched_requirement=None,
                evidence_usable=(
                    existing_document.status == "validated"
                    if existing_document.status in {"validated", "unusable"}
                    else None
                ),
                reason="This document resume event was already processed.",
                idempotent_replay=True,
            )

        current_status = claim.get("status")
        if current_status != ClaimStatus.AWAITING_DOCUMENTS.value:
            raise ClaimResumeError(
                f"Claim {claim_id} cannot resume missing evidence from status "
                f"{current_status!r}; expected awaiting_documents."
            )

        correlation_id = str(uuid4())
        document = new_document.model_copy(
            update={"resume_idempotency_key": idempotency_key}
        )
        if existing_document is None:
            self._repository.add_document(document)
            self._append_event(
                claim_id,
                document,
                action="document_received",
                from_status=current_status,
                to_status=current_status,
                reason=f"Received {document.document_type} for resume processing.",
                correlation_id=correlation_id,
            )
        else:
            document = existing_document

        matched_requirement = match_missing_requirement(
            claim,
            document.document_type,
            requested_action_id=document.requested_action_id,
            replaces_document_id=document.replaces_document_id,
        )
        if matched_requirement is None:
            reason = (
                f"Document type {document.document_type} does not match a currently "
                "missing or unusable requirement."
            )
            self._append_event(
                claim_id,
                document,
                action="missing_requirement_still_unresolved",
                from_status=current_status,
                to_status=current_status,
                reason=reason,
                correlation_id=correlation_id,
            )
            self._repository.mark_document_resume_processed(
                claim_id,
                document.document_id,
                idempotency_key=idempotency_key,
                result_status=current_status,
            )
            return ResumeClaimResult(
                claim_id=claim_id,
                document_id=document.document_id,
                previous_status=current_status,
                final_status=current_status,
                matched_requirement=None,
                evidence_usable=None,
                reason=reason,
            )

        extraction = _normalize_extraction(
            extraction_result
            or self._document_extractor.extract(document, matched_requirement),
            document=document,
            matched_requirement=matched_requirement,
        )
        explicit_replacement = bool(
            document.requested_action_id and document.replaces_document_id
        )
        if extraction.usable and explicit_replacement:
            # Successful replacement acceptance is committed with action
            # consumption and target supersession in save_review_result.
            pass
        elif extraction.usable:
            self._repository.mark_document_validated(
                claim_id,
                document.document_id,
                quality_reason=extraction.reason,
                supported_capabilities=extraction.supported_capabilities,
                evidence_findings=extraction.evidence_findings,
            )
        else:
            self._repository.mark_document_unusable(
                claim_id,
                document.document_id,
                extraction.reason,
                supported_capabilities=extraction.supported_capabilities,
                evidence_findings=extraction.evidence_findings,
            )

        self._append_event(
            claim_id,
            document,
            action="document_quality_checked",
            from_status=current_status,
            to_status=current_status,
            reason=extraction.reason,
            correlation_id=correlation_id,
            extra_details={"usable": extraction.usable},
        )

        resolution_action = (
            "missing_requirement_satisfied"
            if extraction.usable
            else "missing_requirement_still_unresolved"
        )
        self._append_event(
            claim_id,
            document,
            action=resolution_action,
            from_status=current_status,
            to_status=current_status,
            reason=extraction.reason,
            correlation_id=correlation_id,
            extra_details={"requirement": matched_requirement},
        )

        self._repository.update_claim_status(
            claim_id, ClaimStatus.REVIEW_PROCESSING
        )
        self._append_event(
            claim_id,
            document,
            action="claim_review_resumed",
            from_status=current_status,
            to_status=ClaimStatus.REVIEW_PROCESSING.value,
            reason="The downstream review stage resumed for the new evidence.",
            correlation_id=correlation_id,
        )

        intake_result = intake_result_from_claim(claim)
        documents = self._repository.get_documents(claim_id)
        documents = _replace_document_status(
            documents,
            document.document_id,
            "validated" if extraction.usable else "unusable",
            extraction,
        )
        review_documents = _documents_for_resumed_review(
            documents, document, extraction
        )
        metadata = _build_review_metadata(
            claim=claim,
            documents=review_documents,
            conflicts=extraction.conflicts,
        )
        review_result = self._review_service.review(
            intake_result,
            metadata,
            evidence_parts=_build_review_evidence_parts(review_documents),
        )
        final_status = self._repository.save_review_result(
            claim_id,
            review_result,
            correlation_id=correlation_id,
            resume_document_id=document.document_id,
            resume_idempotency_key=idempotency_key,
            replacement_document=(
                next(
                    item
                    for item in review_documents
                    if item.document_id == document.document_id
                )
                if extraction.usable and explicit_replacement
                else None
            ),
            retry_replacement_action_id=(
                document.requested_action_id
                if explicit_replacement and not extraction.usable
                else None
            ),
            review_generation_key=idempotency_key,
        )

        move_action = {
            ClaimStatus.AWAITING_DOCUMENTS: "missing_requirement_still_unresolved",
            ClaimStatus.INSPECTION_PENDING: "claim_moved_to_inspection_pending",
            ClaimStatus.HUMAN_REVIEW_REQUIRED: "claim_moved_to_human_review",
        }[final_status]
        review_details: dict[str, object] = {}
        if final_status == ClaimStatus.HUMAN_REVIEW_REQUIRED:
            current_claim = self._repository.get_claim(claim_id) or {}
            if current_claim.get("current_human_review_id"):
                review_details["review_id"] = current_claim[
                    "current_human_review_id"
                ]
            if current_claim.get("current_human_review_generation"):
                review_details["review_generation"] = current_claim[
                    "current_human_review_generation"
                ]
        self._append_event(
            claim_id,
            document,
            action=move_action,
            from_status=ClaimStatus.REVIEW_PROCESSING.value,
            to_status=final_status.value,
            reason=review_result.priority_reason,
            correlation_id=correlation_id,
            extra_details=review_details,
        )

        return ResumeClaimResult(
            claim_id=claim_id,
            document_id=document.document_id,
            previous_status=current_status,
            final_status=final_status.value,
            matched_requirement=matched_requirement,
            evidence_usable=extraction.usable,
            reason=extraction.reason,
        )

    def _append_event(
        self,
        claim_id: str,
        document: ClaimDocument,
        *,
        action: str,
        from_status: str,
        to_status: str,
        reason: str,
        correlation_id: str,
        extra_details: dict[str, object] | None = None,
    ) -> None:
        details: dict[str, object] = {"reason": reason}
        details.update(extra_details or {})
        self._repository.append_claim_event(
            claim_id,
            action=action,
            actor="firstnoticeai",
            from_status=from_status,
            to_status=to_status,
            details=details,
            correlation_id=correlation_id,
            document_id=document.document_id,
            event_id=f"{document.document_id}-{action}",
        )


def match_missing_requirement(
    claim: dict[str, object],
    document_type: str,
    *,
    requested_action_id: str | None = None,
    replaces_document_id: str | None = None,
) -> str | None:
    missing_types = {
        str(item.get("type"))
        for item in claim.get("missing_documents", [])
        if isinstance(item, dict) and item.get("type")
    }
    unusable_types = {
        str(item.get("evidence_type"))
        for item in claim.get("unusable_evidence", [])
        if isinstance(item, dict) and item.get("evidence_type")
    }
    unresolved = missing_types | unusable_types

    if requested_action_id and replaces_document_id:
        action = next(
            (
                item
                for item in parse_requested_actions(
                    claim.get("requested_actions", [])
                )
                if isinstance(item, UploadDocumentRequestedAction)
                and item.action_id == requested_action_id
            ),
            None,
        )
        if (
            action is not None
            and action.document_type == document_type
            and action.replaces_document_id == replaces_document_id
        ):
            return document_type

    if document_type in unresolved:
        return document_type
    if document_type == "license_plate_photo" and "vehicle_identity" in unresolved:
        return "vehicle_identity"
    if document_type.startswith("police_report_page_"):
        if document_type in unresolved:
            return document_type
        if "police_report" in unresolved:
            return "police_report"
    return None


def _replace_document_status(
    documents: list[ClaimDocument],
    document_id: str,
    status: str,
    extraction: DocumentExtractionResult,
) -> list[ClaimDocument]:
    return [
        document.model_copy(
            update={
                "status": status,
                "quality_reason": extraction.reason,
                "supported_capabilities": extraction.supported_capabilities,
                "evidence_findings": extraction.evidence_findings,
            }
        )
        if document.document_id == document_id
        else document
        for document in documents
    ]


def _build_review_metadata(
    *,
    claim: dict[str, object],
    documents: list[ClaimDocument],
    conflicts,
) -> ClaimEvidenceMetadata:
    active_documents = [
        document for document in documents if document.status != "superseded"
    ]
    uploaded_by_key: dict[tuple[str, str], UploadedEvidence] = {}
    for document in sorted(active_documents, key=lambda item: item.document_id):
        usable = (
            True
            if document.status == "validated"
            else False if document.status == "unusable" else None
        )
        quality = [document.quality_reason] if document.quality_reason else []
        for evidence_type in dict.fromkeys(
            [document.document_type, *document.supported_capabilities]
        ):
            uploaded_by_key[(document.filename, evidence_type)] = UploadedEvidence(
                evidence_type=evidence_type,
                filename=document.filename,
                document_id=document.document_id,
                source_identity=f"document:{document.document_id}",
                document_type=document.document_type,
                evidence_generation=document.document_id,
                status=document.status,
                usable=usable,
                quality_observations=quality,
                evidence_findings=document.evidence_findings,
            )

    active_filenames = {document.filename for document in active_documents}
    documents_by_filename: dict[str, ClaimDocument | None] = {}
    for document in sorted(active_documents, key=lambda item: item.document_id):
        documents_by_filename[document.filename] = (
            None
            if document.filename in documents_by_filename
            else document
        )
    for item in claim.get("image_evidence_capabilities", []):
        if not isinstance(item, dict) or item.get("source") not in active_filenames:
            continue
        filename = str(item["source"])
        source_document = documents_by_filename.get(filename)
        if source_document is None:
            continue
        quality = [str(value) for value in item.get("quality_observations", [])]
        for evidence_type in item.get("supported_capabilities", []):
            uploaded_by_key.setdefault(
                (filename, str(evidence_type)),
                UploadedEvidence(
                    evidence_type=str(evidence_type),
                    filename=filename,
                    document_id=source_document.document_id,
                    source_identity=f"document:{source_document.document_id}",
                    document_type=source_document.document_type,
                    evidence_generation=source_document.document_id,
                    status=source_document.status,
                    usable=True,
                    quality_observations=quality,
                ),
            )

    uploaded = [uploaded_by_key[key] for key in sorted(uploaded_by_key)]
    valid_license_plate = any(
        document.status == "validated"
        and bool(
            {"license_plate_photo", "vehicle_identity"}
            & set(document.supported_capabilities)
        )
        for document in active_documents
    )
    identity_artifact_present = any(
        document.document_type == "license_plate_photo"
        or bool(
            {"license_plate_photo", "vehicle_identity"}
            & set(document.supported_capabilities)
        )
        for document in active_documents
    )
    license_plate_unresolved = any(
        item.get("type") in {"license_plate_photo", "vehicle_identity"}
        for item in claim.get("missing_documents", [])
        if isinstance(item, dict)
    )

    return ClaimEvidenceMetadata(
        uploaded_evidence=uploaded,
        police_attended=any(
            item.get("type") == "police_report"
            for item in claim.get("missing_documents", [])
            if isinstance(item, dict)
        ),
        vehicle_towed=any(
            item.get("type") == "towing_receipt"
            for item in claim.get("missing_documents", [])
            if isinstance(item, dict)
        ),
        vehicle_identity_clear=(
            True
            if valid_license_plate
            else False
            if license_plate_unresolved or identity_artifact_present
            else None
        ),
        known_conflicts=conflicts,
        approved_issue_fingerprints=[
            str(value) for value in claim.get("approved_issue_fingerprints", [])
        ],
    )


def _normalize_extraction(
    extraction: DocumentExtractionResult,
    *,
    document: ClaimDocument,
    matched_requirement: str,
) -> DocumentExtractionResult:
    capabilities = list(extraction.supported_capabilities)
    identity_capabilities = {"license_plate_photo", "vehicle_identity"}
    if extraction.usable and identity_capabilities & set(capabilities):
        capabilities.extend(["license_plate_photo", "vehicle_identity"])
    elif extraction.usable and matched_requirement in {
        "damage_evidence",
        "vehicle_identity",
        "license_plate_photo",
    }:
        capabilities.append(matched_requirement)
    return extraction.model_copy(
        update={"supported_capabilities": list(dict.fromkeys(capabilities))}
    )


def _documents_for_resumed_review(
    documents: list[ClaimDocument],
    new_document: ClaimDocument,
    extraction: DocumentExtractionResult,
) -> list[ClaimDocument]:
    replaced_id = new_document.replaces_document_id if extraction.usable else None
    return [
        document
        for document in documents
        if document.status != "superseded" and document.document_id != replaced_id
    ]


def _build_review_evidence_parts(
    documents: list[ClaimDocument],
) -> list[types.Part]:
    parts: list[types.Part] = []
    ordered = sorted(
        (document for document in documents if document.status != "superseded"),
        key=lambda document: document.document_id,
    )
    for document in ordered:
        if not document.storage_path:
            continue
        try:
            raw_part = evidence_part(
                document.storage_path,
                mime_type=document.content_type,
            )
        except (FileNotFoundError, ValueError):
            continue
        parts.extend(
            [
                types.Part.from_text(
                    text=(
                        f"Evidence source: {document.filename}\n"
                        f"Audit document type: {document.document_type}"
                    )
                ),
                raw_part,
            ]
        )
    return parts
