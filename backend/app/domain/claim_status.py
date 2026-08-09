from enum import StrEnum

from app.models.review_result import ReviewResult


class ClaimStatus(StrEnum):
    NEW = "new"
    INTAKE_COMPLETE = "intake_complete"
    REVIEW_PROCESSING = "review_processing"
    AWAITING_DOCUMENTS = "awaiting_documents"
    INSPECTION_READY = "inspection_ready"
    INSPECTION_PENDING = "inspection_pending"
    INSPECTION_SCHEDULED = "inspection_scheduled"
    ADJUSTER_NOTIFIED = "adjuster_notified"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


ALLOWED_TRANSITIONS: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.NEW: frozenset({ClaimStatus.INTAKE_COMPLETE}),
    ClaimStatus.INTAKE_COMPLETE: frozenset({ClaimStatus.REVIEW_PROCESSING}),
    ClaimStatus.REVIEW_PROCESSING: frozenset(
        {
            ClaimStatus.AWAITING_DOCUMENTS,
            ClaimStatus.INSPECTION_READY,
            ClaimStatus.HUMAN_REVIEW_REQUIRED,
        }
    ),
    ClaimStatus.AWAITING_DOCUMENTS: frozenset(
        {
            ClaimStatus.AWAITING_DOCUMENTS,
            ClaimStatus.REVIEW_PROCESSING,
        }
    ),
    ClaimStatus.INSPECTION_READY: frozenset(
        {
            ClaimStatus.AWAITING_DOCUMENTS,
            ClaimStatus.INSPECTION_PENDING,
        }
    ),
    ClaimStatus.INSPECTION_PENDING: frozenset(
        {ClaimStatus.INSPECTION_SCHEDULED}
    ),
    ClaimStatus.INSPECTION_SCHEDULED: frozenset(
        {ClaimStatus.ADJUSTER_NOTIFIED}
    ),
    ClaimStatus.HUMAN_REVIEW_REQUIRED: frozenset(
        {ClaimStatus.REVIEW_PROCESSING}
    ),
}


class InvalidClaimStatusTransition(ValueError):
    """Raised when code attempts a transition outside the workflow graph."""


def validate_claim_status_transition(
    from_status: str | ClaimStatus,
    to_status: str | ClaimStatus,
) -> tuple[ClaimStatus, ClaimStatus]:
    try:
        current = ClaimStatus(from_status)
        target = ClaimStatus(to_status)
    except ValueError as exc:
        raise InvalidClaimStatusTransition(
            f"Unknown claim status transition: {from_status!s} -> {to_status!s}"
        ) from exc

    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidClaimStatusTransition(
            f"Claim status transition is not allowed: {current.value} -> {target.value}"
        )

    return current, target


def review_target_status(review_result: ReviewResult) -> ClaimStatus:
    if review_result.requires_human_review:
        return ClaimStatus.HUMAN_REVIEW_REQUIRED
    if not review_result.intake_complete:
        return ClaimStatus.AWAITING_DOCUMENTS
    return ClaimStatus.INSPECTION_READY
