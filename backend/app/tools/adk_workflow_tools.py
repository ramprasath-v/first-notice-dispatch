from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.adk.tools import FunctionTool

from app.models.adk_orchestration import ClaimStateResult, EvidenceInput
from app.models.claim_document import ClaimDocument
from app.models.intake_result import (
    ImageEvidenceCapabilities,
    IntakeResult,
    intake_result_from_claim,
)
from app.models.review_result import (
    ClaimEvidenceMetadata,
    EvidenceConflict,
    UploadedEvidence,
)
from app.services.claim_review_service import ClaimReviewService
from app.services.intake_extraction_service import evidence_part
from app.tools.firestore_repository import (
    FirestoreClaimRepository,
    generate_document_id,
)
from app.workflows.claim_dispatch_workflow import ClaimDispatchWorkflow
from app.workflows.claim_resume_workflow import ClaimResumeWorkflow


class ClaimWorkflowToolAdapter:
    """Small ADK-facing adapters over the existing deterministic capabilities."""

    def __init__(
        self,
        *,
        repository: FirestoreClaimRepository,
        review_service: ClaimReviewService,
        resume_workflow: ClaimResumeWorkflow,
        dispatch_workflow: ClaimDispatchWorkflow,
    ) -> None:
        self.repository = repository
        self.review_service = review_service
        self.resume_workflow = resume_workflow
        self.dispatch_workflow = dispatch_workflow

    def get_claim_state(self, claim_id: str) -> ClaimStateResult:
        """Reload the durable claim state from Firestore."""
        claim = self.repository.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"Claim {claim_id} does not exist.")
        return _claim_state_result(claim)

    def create_claim_record(
        self,
        intake_result: IntakeResult,
        evidence_inputs: list[EvidenceInput],
    ) -> ClaimStateResult:
        """Persist validated intake and file metadata without storing raw bytes."""
        claim_id = self.repository.save_completed_intake(intake_result)
        received_at = datetime.now(timezone.utc)
        for evidence in evidence_inputs:
            path = Path(evidence.path).expanduser().resolve()
            self.repository.add_document(
                ClaimDocument(
                    document_id=generate_document_id(),
                    claim_id=claim_id,
                    document_type=evidence.document_type,
                    filename=path.name,
                    storage_path=str(path),
                    received_at=received_at,
                )
            )
        return self.get_claim_state(claim_id)

    def get_claim_evidence_inputs(self, claim_id: str) -> list[EvidenceInput]:
        """Load persisted evidence references for an event-driven intake."""
        return [
            EvidenceInput(
                path=str(document.storage_path),
                document_type=document.document_type,
                content_type=document.content_type,
            )
            for document in self.repository.get_documents(claim_id)
            if document.status != "superseded" and document.storage_path
        ]

    def get_claim_intake_context(self, claim_id: str) -> dict[str, str | None]:
        claim = self.repository.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"Claim {claim_id} does not exist.")
        return {
            "incident_description": claim.get("incident_description"),
            "policy_number_hint": claim.get("policy_number_hint"),
        }

    def complete_claim_intake(
        self, claim_id: str, intake_result: IntakeResult
    ) -> ClaimStateResult:
        self.repository.complete_claim_shell_intake(claim_id, intake_result)
        return self.get_claim_state(claim_id)

    def run_claim_review(self, claim_id: str) -> ClaimStateResult:
        """Run only the existing review stage and persist its deterministic route."""
        claim = self.repository.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"Claim {claim_id} does not exist.")
        if claim.get("status") != "intake_complete":
            raise ValueError(
                f"Claim {claim_id} cannot review from status {claim.get('status')!r}."
            )

        documents = self.repository.get_documents(claim_id)
        active_documents = sorted([
            document for document in documents if document.status != "superseded"
        ], key=lambda document: document.document_id)
        intake_result = intake_result_from_claim(claim)
        metadata = build_initial_review_metadata(
            intake_result,
            active_documents,
            policy_number_hint=claim.get("policy_number_hint"),
        )
        evidence_parts = []
        seen_paths: set[str] = set()
        for document in active_documents:
            if not document.storage_path or document.storage_path in seen_paths:
                continue
            try:
                evidence_parts.append(
                    evidence_part(
                        document.storage_path,
                        mime_type=document.content_type,
                    )
                )
                seen_paths.add(document.storage_path)
            except (FileNotFoundError, ValueError):
                continue

        self.repository.update_claim_status(claim_id, "review_processing")
        review_result = self.review_service.review(
            intake_result,
            metadata,
            evidence_parts=evidence_parts,
        )
        self.repository.save_review_result(
            claim_id,
            review_result,
            review_generation_key=f"{claim_id}:submitted-review:v1",
        )
        return self.get_claim_state(claim_id)

    def request_missing_evidence(self, claim_id: str) -> dict[str, Any]:
        """Return the deterministic unresolved evidence list; no real message is sent."""
        claim = self.repository.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"Claim {claim_id} does not exist.")
        return {
            "claim_id": claim_id,
            "status": claim.get("status"),
            "missing_documents": claim.get("missing_documents", []),
            "unusable_evidence": claim.get("unusable_evidence", []),
        }

    def resume_claim_with_document(
        self, claim_id: str, document: ClaimDocument
    ) -> dict[str, Any]:
        """Invoke the existing idempotent missing-evidence resume workflow."""
        return self.resume_workflow.resume(claim_id, document).model_dump(
            mode="python"
        )

    def dispatch_to_adjuster(self, claim_id: str) -> dict[str, Any]:
        """Invoke existing idempotent scheduling and adjuster dispatch."""
        return self.dispatch_workflow.dispatch(claim_id).model_dump(mode="python")

    def function_tools(self) -> list[FunctionTool]:
        """Expose deterministic adapters as official ADK FunctionTool objects."""
        return [
            FunctionTool(self.get_claim_state),
            FunctionTool(self.create_claim_record),
            FunctionTool(self.run_claim_review),
            FunctionTool(self.request_missing_evidence),
            FunctionTool(self.resume_claim_with_document),
            FunctionTool(self.dispatch_to_adjuster),
        ]


def _claim_state_result(claim: dict[str, Any]) -> ClaimStateResult:
    return ClaimStateResult(
        claim_id=str(claim["claim_id"]),
        status=str(claim["status"]),
        missing_documents=[
            str(item.get("type"))
            for item in claim.get("missing_documents", [])
            if isinstance(item, dict) and item.get("type")
        ],
        requires_human_review=bool(claim.get("requires_human_review", False)),
    )


def build_initial_review_metadata(
    intake_result: IntakeResult,
    documents: list[ClaimDocument],
    *,
    policy_number_hint: str | None = None,
) -> ClaimEvidenceMetadata:
    """Translate image content facts into deterministic review capabilities."""
    evidence_by_key: dict[tuple[str, str], UploadedEvidence] = {}
    matched_image_facts: list[ImageEvidenceCapabilities] = []

    for document in sorted(documents, key=lambda item: item.document_id):
        fact = _capabilities_for_document(
            document, intake_result.image_evidence_capabilities
        )
        if fact is not None:
            matched_image_facts.append(fact)
        observations = [document.quality_reason] if document.quality_reason else []
        if fact is not None:
            observations.extend(fact.quality_observations)
        usable = (
            True
            if document.status == "validated"
            else False if document.status == "unusable" else None
        )
        if fact is not None:
            if document.document_type in fact.supported_capabilities:
                usable = True
            elif document.document_type in fact.unusable_capabilities:
                usable = False
        evidence_by_key[(document.filename, document.document_type)] = UploadedEvidence(
            evidence_type=document.document_type,
            filename=document.filename,
            document_id=document.document_id,
            source_identity=f"document:{document.document_id}",
            document_type=document.document_type,
            evidence_generation=document.document_id,
            status=document.status,
            usable=usable,
            quality_observations=list(dict.fromkeys(observations)),
        )
        if fact is not None:
            for capability in fact.supported_capabilities:
                evidence_by_key[(document.filename, capability)] = UploadedEvidence(
                    evidence_type=capability,
                    filename=document.filename,
                    document_id=document.document_id,
                    source_identity=f"document:{document.document_id}",
                    document_type=document.document_type,
                    evidence_generation=document.document_id,
                    status=document.status,
                    usable=True,
                    quality_observations=list(
                        dict.fromkeys(fact.quality_observations)
                    ),
                )

    identity_supported = any(
        capability in fact.supported_capabilities
        for fact in matched_image_facts
        for capability in ("vehicle_identity", "license_plate_photo")
    )
    dedicated_plate = any(
        document.document_type == "license_plate_photo" for document in documents
    )
    identity_clear = (
        True
        if identity_supported
        else False if matched_image_facts or dedicated_plate else None
    )
    known_conflicts = []
    if (
        policy_number_hint
        and intake_result.policy_number
        and _normalize_identifier(policy_number_hint)
        != _normalize_identifier(intake_result.policy_number)
    ):
        known_conflicts.append(
            EvidenceConflict(
                field="policy_number",
                values=[policy_number_hint, intake_result.policy_number],
                sources=["claimant submission", "submitted evidence"],
                reason=(
                    "The claimant-provided policy hint differs from the policy "
                    "identifier extracted from submitted evidence."
                ),
            )
        )
    return ClaimEvidenceMetadata(
        uploaded_evidence=[
            evidence_by_key[key] for key in sorted(evidence_by_key)
        ],
        vehicle_identity_clear=identity_clear,
        known_conflicts=known_conflicts,
    )


def _normalize_identifier(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _capabilities_for_document(
    document: ClaimDocument,
    facts: list[ImageEvidenceCapabilities],
) -> ImageEvidenceCapabilities | None:
    filename = document.filename.lower()
    for fact in facts:
        source_name = fact.source.rstrip("/").rsplit("/", 1)[-1].lower()
        if source_name == filename:
            return fact
    return None
