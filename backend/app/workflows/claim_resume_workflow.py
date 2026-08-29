from uuid import uuid4

from google.genai import types

from app.domain.claim_status import ClaimStatus
from app.domain.evidence_reasoning import (
    REPLACEABLE_DOCUMENT_TYPES,
    select_corroborated_image_outlier,
)
from app.models.claim_document import (
    ClaimDocument,
    DocumentExtractionResult,
    ResumeClaimResult,
)
from app.models.intake_result import IntakeResult, intake_result_from_claim
from app.models.review_result import (
    ClaimEvidenceMetadata,
    EvidenceConflict,
    ReviewResult,
    UploadedEvidence,
)
from app.models.requested_action import (
    UploadDocumentRequestedAction,
    parse_requested_actions,
)
from app.services.claim_review_service import ClaimReviewService
from app.services.document_extraction_service import (
    DocumentExtractor,
    UnsupportedResumeDocumentTypeError,
)
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
        try:
            return self._resume_once(
                claim_id,
                new_document,
                extraction_result=extraction_result,
            )
        except Exception as exc:
            persisted = self._repository.get_document(
                claim_id, new_document.document_id
            )
            if (
                persisted is not None
                and persisted.resume_started_at is not None
                and persisted.resume_processed_at is None
            ):
                if isinstance(exc, UnsupportedResumeDocumentTypeError):
                    self._repository.mark_document_resume_rejected(
                        claim_id,
                        new_document.document_id,
                        error_type=type(exc).__name__,
                    )
                else:
                    self._repository.mark_document_resume_retry_required(
                        claim_id,
                        new_document.document_id,
                        error_type=type(exc).__name__,
                    )
            raise

    def _resume_once(
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
        same_operation_retry = (
            current_status == ClaimStatus.REVIEW_PROCESSING.value
            and existing_document is not None
            and claim.get("active_resume_document_id") == new_document.document_id
            and claim.get("active_resume_idempotency_key") == idempotency_key
            and existing_document.resume_idempotency_key == idempotency_key
            and claim.get("active_resume_correlation_id")
            == existing_document.resume_correlation_id
        )
        if (
            current_status != ClaimStatus.AWAITING_DOCUMENTS.value
            and not same_operation_retry
        ):
            raise ClaimResumeError(
                f"Claim {claim_id} cannot resume missing evidence from status "
                f"{current_status!r}; expected awaiting_documents or the same "
                "in-progress document resume operation."
            )

        correlation_id = (
            str(claim.get("active_resume_correlation_id"))
            if same_operation_retry
            else str(uuid4())
        )
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

        document = _bind_server_authorized_replacement(claim, document)

        matched_requirement = (
            document.resume_matched_requirement
            if same_operation_retry
            else match_missing_requirement(
                claim,
                document.document_type,
                requested_action_id=document.requested_action_id,
                replaces_document_id=document.replaces_document_id,
            )
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

        pending_upload_actions = [
            action
            for action in parse_requested_actions(claim.get("requested_actions", []))
            if isinstance(action, UploadDocumentRequestedAction)
        ]
        current_action = next(
            (
                action
                for action in pending_upload_actions
                if action.action_id == document.requested_action_id
            ),
            None,
        )

        if not same_operation_retry:
            self._repository.begin_document_resume_review(
                claim_id,
                document.document_id,
                idempotency_key=idempotency_key,
                matched_requirement=matched_requirement,
                correlation_id=correlation_id,
                replacement_action_id=(
                    document.requested_action_id
                    if document.requested_action_id
                    and document.replaces_document_id
                    else None
                ),
                replaces_document_id=document.replaces_document_id,
                replacement_document_type=(
                    document.document_type
                    if document.requested_action_id
                    and document.replaces_document_id
                    else None
                ),
            )

        extraction = document.resume_extraction_result
        if extraction is None:
            extraction = _normalize_extraction(
                extraction_result
                or self._document_extractor.extract(document, matched_requirement),
                document=document,
                matched_requirement=matched_requirement,
            )
            mismatch = _requested_vehicle_identity_mismatch(
                action=current_action,
                current_document=document,
                extraction=extraction,
                documents=self._repository.get_documents(claim_id),
            )
            if mismatch is not None:
                extraction = extraction.model_copy(
                    update={
                        "usable": False,
                        "reason": mismatch.reason,
                        "satisfies_requirement": None,
                        "conflicts": [*extraction.conflicts, mismatch],
                    }
                )
            self._repository.save_document_resume_extraction(
                claim_id, document.document_id, extraction
            )
        explicit_replacement = bool(
            document.requested_action_id and document.replaces_document_id
        )
        quality_already_processed = (
            same_operation_retry and document.resume_quality_processed_at is not None
        )
        if quality_already_processed:
            pass
        elif extraction.usable and explicit_replacement:
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
                evidence_facts=(
                    extraction.evidence_facts.fact_values()
                    if extraction.evidence_facts
                    else {}
                ),
            )
        else:
            self._repository.mark_document_unusable(
                claim_id,
                document.document_id,
                extraction.reason,
                supported_capabilities=extraction.supported_capabilities,
                evidence_findings=extraction.evidence_findings,
                evidence_facts=(
                    extraction.evidence_facts.fact_values()
                    if extraction.evidence_facts
                    else {}
                ),
            )

        if not quality_already_processed:
            self._append_event(
                claim_id,
                document,
                action="document_quality_checked",
                from_status=ClaimStatus.AWAITING_DOCUMENTS.value,
                to_status=ClaimStatus.AWAITING_DOCUMENTS.value,
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
                from_status=ClaimStatus.AWAITING_DOCUMENTS.value,
                to_status=ClaimStatus.AWAITING_DOCUMENTS.value,
                reason=extraction.reason,
                correlation_id=correlation_id,
                extra_details={"requirement": matched_requirement},
            )
            self._repository.mark_document_resume_quality_processed(
                claim_id, document.document_id
            )

        if current_action is not None and (
            not extraction.usable or len(pending_upload_actions) > 1
        ):
            remaining_actions = [
                action
                for action in pending_upload_actions
                if action.action_id != current_action.action_id
            ]
            self._repository.complete_requested_evidence_item(
                claim_id=claim_id,
                document=document.model_copy(
                    update={
                        "status": "validated" if extraction.usable else "unusable",
                        "quality_reason": extraction.reason,
                        "supported_capabilities": extraction.supported_capabilities,
                        "evidence_findings": extraction.evidence_findings,
                        "evidence_facts": (
                            extraction.evidence_facts.fact_values()
                            if extraction.evidence_facts
                            else {}
                        ),
                    }
                ),
                extraction=extraction,
                remaining_actions=remaining_actions,
                idempotency_key=idempotency_key,
                retry_action=(
                    current_action.model_copy(
                        update={"replaces_document_id": document.document_id}
                    )
                    if not extraction.usable
                    and any(
                        conflict.field == "vehicle_identity"
                        for conflict in extraction.conflicts
                    )
                    else None
                ),
            )
            return ResumeClaimResult(
                claim_id=claim_id,
                document_id=document.document_id,
                previous_status=current_status,
                final_status=ClaimStatus.AWAITING_DOCUMENTS.value,
                matched_requirement=matched_requirement,
                evidence_usable=extraction.usable,
                reason=(
                    "Requested evidence was accepted; additional items remain."
                    if extraction.usable
                    else "The submitted evidence was unusable; the request remains open."
                ),
            )

        if not quality_already_processed:
            self._append_event(
                claim_id,
                document,
                action="claim_review_resumed",
                from_status=ClaimStatus.AWAITING_DOCUMENTS.value,
                to_status=ClaimStatus.REVIEW_PROCESSING.value,
                reason="All requested evidence is available; Review resumed.",
                correlation_id=correlation_id,
            )

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
        intake_result = _current_review_intake_result(claim, review_documents)
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
        replacement_document = (
            next(
                item
                for item in review_documents
                if item.document_id == document.document_id
            )
            if extraction.usable and explicit_replacement
            else None
        )
        if extraction.usable and not explicit_replacement:
            reconciled = _reconcile_current_document_as_replacement(
                current_document=next(
                    item
                    for item in review_documents
                    if item.document_id == document.document_id
                ),
                review_result=review_result,
                metadata=metadata,
            )
            if reconciled is not None:
                document = reconciled
                documents = [
                    reconciled
                    if item.document_id == reconciled.document_id
                    else item
                    for item in documents
                ]
                review_documents = _documents_for_resumed_review(
                    documents, reconciled, extraction
                )
                review_result = self._review_service.review(
                    _current_review_intake_result(claim, review_documents),
                    _build_review_metadata(
                        claim=claim,
                        documents=review_documents,
                        conflicts=[],
                    ),
                    evidence_parts=_build_review_evidence_parts(review_documents),
                )
                replacement_document = reconciled
        final_status = self._repository.save_review_result(
            claim_id,
            review_result,
            correlation_id=correlation_id,
            resume_document_id=document.document_id,
            resume_idempotency_key=idempotency_key,
            replacement_document=replacement_document,
            retry_replacement_action_id=(
                document.requested_action_id
                if explicit_replacement and not extraction.usable
                else None
            ),
            review_generation_key=idempotency_key,
        )

        move_action = {
            ClaimStatus.AWAITING_DOCUMENTS: "missing_requirement_still_unresolved",
            ClaimStatus.INSPECTION_READY: "claim_moved_to_inspection_ready",
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

    if requested_action_id:
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


def _bind_server_authorized_replacement(
    claim: dict[str, object], document: ClaimDocument
) -> ClaimDocument:
    """Bind a resume artifact only to a unique persisted replacement action.

    The persisted claim action remains authoritative. This also safely recovers
    uploads from an older claimant client that omitted requested_action_id.
    """
    upload_actions = [
        action
        for action in parse_requested_actions(claim.get("requested_actions", []))
        if isinstance(action, UploadDocumentRequestedAction)
    ]
    if document.requested_action_id:
        matches = [
            action
            for action in upload_actions
            if action.action_id == document.requested_action_id
        ]
        if len(matches) != 1:
            raise ClaimResumeError(
                "The document replacement action is missing or already consumed."
            )
        action = matches[0]
    else:
        replacement_actions = [
            action for action in upload_actions if action.replaces_document_id
        ]
        if not (
            len(replacement_actions) == 1
            and document.document_type in REPLACEABLE_DOCUMENT_TYPES
            and replacement_actions[0].document_type
            in REPLACEABLE_DOCUMENT_TYPES
        ):
            return document
        action = replacement_actions[0]

    return document.model_copy(
        update={
            "requested_action_id": action.action_id,
            "replaces_document_id": action.replaces_document_id,
            "document_type": action.document_type,
        }
    )


def _reconcile_current_document_as_replacement(
    *,
    current_document: ClaimDocument,
    review_result: ReviewResult,
    metadata: ClaimEvidenceMetadata,
) -> ClaimDocument | None:
    """Bind current evidence only when deterministic review proves its target."""
    if current_document.replaces_document_id:
        return None
    capabilities = set(current_document.supported_capabilities)
    if "damage_evidence" not in capabilities or not (
        {"license_plate_photo", "vehicle_identity"} & capabilities
    ):
        return None

    outlier = select_corroborated_image_outlier(
        review_result.conflicts,
        review_result.unresolved_uncertainties,
        review_result.current_evidence_findings,
        metadata.uploaded_evidence,
    )
    if (
        outlier is None
        or outlier.document_id == current_document.document_id
        or current_document.document_id not in outlier.corroborating_document_ids
    ):
        return None

    actions = [
        action
        for action in review_result.requested_actions
        if isinstance(action, UploadDocumentRequestedAction)
        and action.replaces_document_id == outlier.document_id
    ]
    if len(actions) != 1 or len(review_result.requested_actions) != 1:
        return None
    if actions[0].document_type not in {
        "damage_evidence",
        "license_plate_photo",
    }:
        return None

    assessments = [
        *review_result.source_aware_conflicts,
        *review_result.source_aware_uncertainties,
    ]
    selected_targets = {
        assessment.selected_outlier_document_id
        for assessment in assessments
        if assessment.selected_outlier_document_id
    }
    if selected_targets != {outlier.document_id}:
        return None
    if any(
        uncertainty.source_attribution_incomplete
        for uncertainty in review_result.unresolved_uncertainties
    ):
        return None
    if review_result.missing_documents or review_result.unusable_evidence:
        return None

    return current_document.model_copy(
        update={
            "requested_action_id": actions[0].action_id,
            "replaces_document_id": outlier.document_id,
            "status": "validated",
        }
    )


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
                "evidence_facts": (
                    extraction.evidence_facts.fact_values()
                    if extraction.evidence_facts
                    else {}
                ),
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

    known_conflicts = list(conflicts)
    for field_name, claimant_value in _claimant_entered_facts(claim).items():
        evidence_value = _canonical_active_fact(active_documents, field_name)
        if (
            evidence_value
            and _normalize_fact_value(evidence_value)
            != _normalize_fact_value(claimant_value)
        ):
            known_conflicts.append(EvidenceConflict(
                field=field_name,
                values=[claimant_value, evidence_value],
                sources=["claimant submission", "current active evidence"],
                reason=(
                    "The claimant-provided value differs from the value supported "
                    "by current active evidence."
                ),
            ))

    return ClaimEvidenceMetadata(
        uploaded_evidence=uploaded,
        injury_mentioned=bool(
            (
                claim.get("operational_indicators")
                if isinstance(claim.get("operational_indicators"), dict)
                else {}
            ).get("possible_injury")
        ),
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
        known_conflicts=known_conflicts,
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
    evidence_facts = (
        extraction.evidence_facts.model_copy(update={"source": document.filename})
        if extraction.evidence_facts
        else None
    )
    canonical_findings = (
        evidence_facts.canonical_findings() if evidence_facts else []
    )
    return extraction.model_copy(update={
        "supported_capabilities": list(dict.fromkeys(capabilities)),
        "evidence_facts": evidence_facts,
        "evidence_findings": list(dict.fromkeys([
            *extraction.evidence_findings,
            *canonical_findings,
        ])),
    })


def _requested_vehicle_identity_mismatch(
    *,
    action: UploadDocumentRequestedAction | None,
    current_document: ClaimDocument,
    extraction: DocumentExtractionResult,
    documents: list[ClaimDocument],
) -> EvidenceConflict | None:
    """Reject a requested identity image that contradicts active policy facts."""
    if (
        action is None
        or action.document_type not in {"damage_evidence", "license_plate_photo"}
        or not extraction.usable
        or extraction.evidence_facts is None
        or not (
            {"license_plate_photo", "vehicle_identity"}
            & set(extraction.supported_capabilities)
        )
    ):
        return None

    policy_documents = [
        document
        for document in documents
        if document.document_type == "policy_document"
        and document.status not in {"superseded", "unusable"}
    ]
    incoming = extraction.evidence_facts.fact_values()

    def authoritative(field_name: str) -> tuple[str, list[str]] | None:
        values: dict[str, tuple[str, list[str]]] = {}
        for policy_document in policy_documents:
            value = policy_document.evidence_facts.get(field_name)
            if not value or not value.strip():
                continue
            normalized = _normalize_fact_value(value)
            raw, sources = values.setdefault(normalized, (value.strip(), []))
            sources.append(policy_document.filename)
            values[normalized] = (raw, sources)
        return next(iter(values.values())) if len(values) == 1 else None

    # Strong identifiers are authoritative when both sides provide them, but a
    # match cannot erase a contradiction in other shared structured facts.
    for field_name in ("vin", "license_plate"):
        expected = authoritative(field_name)
        observed = incoming.get(field_name)
        if expected is None or not observed:
            continue
        if _normalize_fact_value(expected[0]) != _normalize_fact_value(observed):
            return EvidenceConflict(
                field="vehicle_identity",
                values=[expected[0], observed],
                sources=[*expected[1], current_document.filename],
                reason=(
                    "The submitted vehicle evidence does not match the insured "
                    f"vehicle {field_name.replace('_', ' ')} in the active policy."
                ),
            )

    for field_name in ("vehicle_make", "vehicle_model", "vehicle_year"):
        expected = authoritative(field_name)
        observed = incoming.get(field_name)
        if (
            expected is not None
            and observed
            and _normalize_fact_value(expected[0])
            != _normalize_fact_value(observed)
        ):
            return EvidenceConflict(
                field="vehicle_identity",
                values=[expected[0], observed],
                sources=[*expected[1], current_document.filename],
                reason=(
                    "The submitted vehicle evidence does not match the insured "
                    "vehicle described by the active policy."
                ),
            )
    return None


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


def _current_review_intake_result(
    claim: dict[str, object], documents: list[ClaimDocument]
) -> IntakeResult:
    """Build source-safe context without replaying superseded intake summaries."""
    active = [document for document in documents if document.status != "superseded"]
    image_capabilities = [
        {
            "source": document.filename,
            "supported_capabilities": list(document.supported_capabilities),
            "unusable_capabilities": [],
            "quality_observations": (
                [document.quality_reason] if document.quality_reason else []
            ),
        }
        for document in active
        if document.content_type is None
        or document.content_type.startswith("image/")
        if document.supported_capabilities
    ]
    evidence_findings = [
        {"source": document.filename, "finding": finding}
        for document in active
        for finding in document.evidence_findings
    ]
    claimant_facts = _claimant_entered_facts(claim)
    return IntakeResult.model_validate({
        "claim_type": claim.get("claim_type"),
        # These three fields were derived by the original multimodal intake and
        # must not survive as current facts after their source is superseded.
        "damage_type": "",
        "parts_affected": [],
        "incident_summary": claim.get("incident_description") or "",
        # Evidence-derived facts are rebuilt from current active artifacts. A
        # claimant-entered value remains explicit and is compared in metadata.
        "policy_number": (
            _current_fact_value(claim, documents, "policy_number", claimant_facts)
        ),
        "incident_date": (
            _current_fact_value(claim, documents, "incident_date", claimant_facts)
        ),
        "vehicle_drivable": claim.get("vehicle_drivable"),
        "uncertainties": [],
        "image_evidence_capabilities": image_capabilities,
        "evidence_findings": evidence_findings,
    })


def _claimant_entered_facts(claim: dict[str, object]) -> dict[str, str]:
    corrections = claim.get("pending_corrections")
    corrected = corrections if isinstance(corrections, dict) else {}
    candidates = {
        "policy_number": corrected.get("policy_number") or claim.get("policy_number_hint"),
        "incident_date": corrected.get("incident_date"),
    }
    return {
        field_name: str(value).strip()
        for field_name, value in candidates.items()
        if value is not None and str(value).strip()
    }


def _canonical_active_fact(
    documents: list[ClaimDocument], field_name: str
) -> str | None:
    values: dict[str, str] = {}
    for document in documents:
        if document.status == "superseded":
            continue
        value = document.evidence_facts.get(field_name)
        if value and value.strip():
            values.setdefault(_normalize_fact_value(value), value.strip())
    return next(iter(values.values())) if len(values) == 1 else None


def _current_fact_value(
    claim: dict[str, object],
    documents: list[ClaimDocument],
    field_name: str,
    claimant_facts: dict[str, str],
) -> object:
    canonical = _canonical_active_fact(documents, field_name)
    if canonical is not None:
        return canonical
    has_scoped_fact = any(
        document.evidence_facts.get(field_name)
        for document in documents
    )
    if has_scoped_fact:
        return claimant_facts.get(field_name)
    # Backward compatibility for claims created before per-artifact facts were
    # persisted. Once scoped facts exist, the claim-level extracted value is no
    # longer used as evidence provenance.
    return claimant_facts.get(field_name) or claim.get(field_name)


def _normalize_fact_value(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _build_review_evidence_parts(
    documents: list[ClaimDocument],
) -> list[types.Part]:
    parts: list[types.Part] = []
    ordered = sorted(
        (
            document
            for document in documents
            if document.status != "superseded"
            and document.document_type != "medical_document"
        ),
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
