import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from google.api_core.exceptions import (
    AlreadyExists,
    GoogleAPICallError,
    PermissionDenied,
    Unauthenticated,
)
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import firestore

from app.domain.claim_status import (
    ClaimStatus,
    review_target_status,
    validate_claim_status_transition,
)
from app.models.intake_result import IntakeResult
from app.models.claim_document import ClaimDocument
from app.models.adjuster_packet import AdjusterPacket
from app.models.inspection_appointment import InspectionAppointment, InspectionSlot
from app.models.notification import AdjusterNotification
from app.models.review_result import ReviewResult
from app.models.human_review import HumanReviewRecord, human_review_id
from app.models.requested_action import (
    UploadDocumentRequestedAction,
    parse_requested_actions,
)


WORKFLOW_VERSION = "1.0"


@dataclass(frozen=True)
class ClaimSubmissionReservation:
    claim_id: str
    event_id: str
    correlation_id: str
    created: bool


@dataclass(frozen=True)
class ReplacementUploadReservation:
    action: UploadDocumentRequestedAction
    document_id: str
    event_id: str
    correlation_id: str
    status: str
    should_upload: bool


@dataclass(frozen=True)
class HumanReviewGeneration:
    generation: int
    generation_key: str
    review_id: str
    created: bool


class ClaimRepositoryError(RuntimeError):
    """Base exception for claim persistence failures."""


class FirestoreAuthenticationError(ClaimRepositoryError):
    """Raised when Application Default Credentials cannot access Firestore."""


class FirestoreWriteError(ClaimRepositoryError):
    """Raised when a Firestore write cannot be completed."""


class FirestoreReadError(ClaimRepositoryError):
    """Raised when a Firestore read cannot be completed."""


class DuplicateClaimError(FirestoreWriteError):
    """Raised when a claim ID already exists."""


class DuplicateDocumentError(FirestoreWriteError):
    """Raised when a claim document ID already exists."""


class DuplicateAppointmentError(FirestoreWriteError):
    """Raised when an inspection appointment ID already exists."""


class DuplicateNotificationError(FirestoreWriteError):
    """Raised when an adjuster notification ID already exists."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_claim_id() -> str:
    """Return a readable claim ID with 32 bits of random entropy."""
    return f"CLM-{secrets.token_hex(4).upper()}"


def generate_document_id() -> str:
    return f"DOC-{secrets.token_hex(4).upper()}"


def intake_result_to_claim_fields(intake_result: IntakeResult) -> dict[str, Any]:
    """Map validated intake data to the approved claim document fields."""
    return {
        "claim_type": intake_result.claim_type,
        "damage_type": intake_result.damage_type,
        "parts_affected": list(intake_result.parts_affected),
        "incident_summary": intake_result.incident_summary,
        "policy_number": intake_result.policy_number,
        "incident_date": intake_result.incident_date,
        "vehicle_drivable": intake_result.vehicle_drivable,
        "image_evidence_capabilities": [
            item.model_dump(mode="python")
            for item in intake_result.image_evidence_capabilities
        ],
        "uncertainties": list(intake_result.uncertainties),
    }


class FirestoreClaimRepository:
    """Firestore persistence boundary for claim state and timeline events."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_default_credentials(
        cls,
        project_id: str,
        database_id: str = "(default)",
    ) -> "FirestoreClaimRepository":

        if not project_id.strip():
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT is required to initialize Firestore."
            )

        if not database_id.strip():
            raise ValueError(
                "FIRESTORE_DATABASE is required to initialize Firestore."
            )

        try:
            return cls(
                firestore.Client(
                    project=project_id,
                    database=database_id,
                )
            )
        except DefaultCredentialsError as exc:
            raise FirestoreAuthenticationError(
                "Firestore authentication failed. Run "
                "'gcloud auth application-default login' and verify "
                "GOOGLE_CLOUD_PROJECT."
            ) from exc
        except Exception as exc:
            raise ClaimRepositoryError(
                f"Firestore client initialization failed: {exc}"
            ) from exc

    def create_claim(
        self,
        intake_result: IntakeResult,
        *,
        status: str = "intake_complete",
        claim_id: str | None = None,
    ) -> str:
        claim_id = claim_id or generate_claim_id()
        now = utc_now()
        data = self._claim_document(
            claim_id=claim_id,
            intake_result=intake_result,
            status=status,
            created_at=now,
            updated_at=now,
        )

        try:
            self._client.collection("claims").document(claim_id).create(data)
        except Exception as exc:
            self._raise_write_error("create claim", exc)

        return claim_id

    def save_intake_result(
        self, claim_id: str, intake_result: IntakeResult
    ) -> None:
        data = intake_result_to_claim_fields(intake_result)
        data["updated_at"] = utc_now()

        try:
            self._client.collection("claims").document(claim_id).update(data)
        except Exception as exc:
            self._raise_write_error("save intake result", exc)

    def update_claim_status(
        self, claim_id: str, status: str | ClaimStatus
    ) -> ClaimStatus:
        claim = self.get_claim(claim_id)
        if claim is None:
            raise FirestoreWriteError(
                f"Could not update claim status: claim {claim_id} does not exist."
            )

        _, target = validate_claim_status_transition(claim.get("status", ""), status)
        try:
            self._client.collection("claims").document(claim_id).update(
                {"status": target.value, "updated_at": utc_now()}
            )
        except Exception as exc:
            self._raise_write_error("update claim status", exc)

        return target

    def append_claim_event(
        self,
        claim_id: str,
        *,
        action: str,
        actor: str,
        from_status: str | None,
        to_status: str | None,
        details: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
        document_id: str | None = None,
        event_id: str | None = None,
        appointment_id: str | None = None,
        notification_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        events_ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("events")
        )
        event_ref = events_ref.document(event_id) if event_id else events_ref.document()
        event = self._event_document(
            action=action,
            actor=actor,
            from_status=from_status,
            to_status=to_status,
            details=details,
            correlation_id=correlation_id,
            document_id=document_id,
            appointment_id=appointment_id,
            notification_id=notification_id,
            idempotency_key=idempotency_key,
        )

        try:
            event_ref.create(event)
        except AlreadyExists:
            if event_id:
                return event_id
            raise
        except Exception as exc:
            self._raise_write_error("append claim event", exc)

        return event_ref.id

    def add_document(self, document: ClaimDocument) -> None:
        document_ref = (
            self._client.collection("claims")
            .document(document.claim_id)
            .collection("documents")
            .document(document.document_id)
        )
        try:
            document_ref.create(document.model_dump(mode="python"))
        except AlreadyExists as exc:
            raise DuplicateDocumentError(
                f"Document {document.document_id} already exists for "
                f"claim {document.claim_id}."
            ) from exc
        except Exception as exc:
            self._raise_write_error("add claim document", exc)

    def get_document(
        self, claim_id: str, document_id: str
    ) -> ClaimDocument | None:
        document_ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("documents")
            .document(document_id)
        )
        try:
            snapshot = document_ref.get()
        except Exception as exc:
            raise FirestoreReadError(
                f"Could not read document {document_id} for claim {claim_id}: {exc}"
            ) from exc

        if not snapshot.exists:
            return None
        return ClaimDocument.model_validate(snapshot.to_dict())

    def get_documents(self, claim_id: str) -> list[ClaimDocument]:
        documents_ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("documents")
        )
        try:
            snapshots = documents_ref.stream()
            return [
                ClaimDocument.model_validate(snapshot.to_dict())
                for snapshot in snapshots
            ]
        except Exception as exc:
            raise FirestoreReadError(
                f"Could not read documents for claim {claim_id}: {exc}"
            ) from exc

    def reserve_replacement_upload(
        self,
        *,
        claim_id: str,
        action_id: str,
        idempotency_key: str,
    ) -> ReplacementUploadReservation:
        claim_ref = self._client.collection("claims").document(claim_id)
        transaction = self._client.transaction()

        @firestore.transactional
        def reserve(transaction):
            snapshot = claim_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise FirestoreWriteError(f"Claim {claim_id} does not exist.")
            claim = snapshot.to_dict()
            if claim.get("status") != ClaimStatus.AWAITING_DOCUMENTS.value:
                raise FirestoreWriteError(
                    "Replacement evidence is not currently accepted for this claim."
                )
            action = next(
                (
                    item
                    for item in parse_requested_actions(
                        claim.get("requested_actions", [])
                    )
                    if isinstance(item, UploadDocumentRequestedAction)
                    and item.action_id == action_id
                ),
                None,
            )
            if action is None:
                raise FirestoreWriteError(
                    "The requested replacement action is missing or already consumed."
                )
            reservations = dict(claim.get("replacement_upload_reservations") or {})
            existing = reservations.get(action_id)
            if isinstance(existing, dict) and existing.get("status") != "retry_required":
                if existing.get("idempotency_key") != idempotency_key:
                    raise FirestoreWriteError(
                        "A replacement upload is already active for this action."
                    )
                return ReplacementUploadReservation(
                    action=action,
                    document_id=str(existing["document_id"]),
                    event_id=str(existing["event_id"]),
                    correlation_id=str(existing["correlation_id"]),
                    status=str(existing["status"]),
                    should_upload=False,
                )

            digest = hashlib.sha256(
                f"{claim_id}:{action_id}:{idempotency_key}".encode("utf-8")
            ).hexdigest()
            reservation = {
                "idempotency_key": idempotency_key,
                "document_id": f"DOC-{digest[:8].upper()}",
                "event_id": f"{claim_id}:{action_id}:upload:{digest[:16]}",
                "correlation_id": f"{action_id}:{digest[16:32]}",
                "status": "uploading",
                "updated_at": utc_now(),
            }
            reservations[action_id] = reservation
            transaction.update(
                claim_ref, {"replacement_upload_reservations": reservations}
            )
            return ReplacementUploadReservation(
                action=action,
                document_id=reservation["document_id"],
                event_id=reservation["event_id"],
                correlation_id=reservation["correlation_id"],
                status="uploading",
                should_upload=True,
            )

        try:
            return reserve(transaction)
        except ClaimRepositoryError:
            raise
        except Exception as exc:
            self._raise_write_error("reserve replacement upload", exc)

    def update_replacement_upload_status(
        self,
        *,
        claim_id: str,
        action_id: str,
        document_id: str,
        status: str,
    ) -> None:
        claim_ref = self._client.collection("claims").document(claim_id)
        transaction = self._client.transaction()

        @firestore.transactional
        def update(transaction):
            snapshot = claim_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise FirestoreWriteError(f"Claim {claim_id} does not exist.")
            claim = snapshot.to_dict()
            reservations = dict(
                claim.get("replacement_upload_reservations") or {}
            )
            current = reservations.get(action_id)
            if not isinstance(current, dict):
                action_still_exists = any(
                    action.action_id == action_id
                    for action in parse_requested_actions(
                        claim.get("requested_actions", [])
                    )
                )
                if not action_still_exists:
                    # The asynchronous resume already consumed the action and
                    # reservation atomically. A late HTTP status update is a no-op.
                    return
                raise FirestoreWriteError(
                    "Replacement upload reservation no longer matches."
                )
            if current.get("document_id") != document_id:
                raise FirestoreWriteError(
                    "Replacement upload reservation no longer matches."
                )
            reservations[action_id] = {
                **current,
                "status": status,
                "updated_at": utc_now(),
            }
            transaction.update(
                claim_ref, {"replacement_upload_reservations": reservations}
            )

        try:
            update(transaction)
        except ClaimRepositoryError:
            raise
        except Exception as exc:
            self._raise_write_error("update replacement upload status", exc)

    def mark_document_unusable(
        self,
        claim_id: str,
        document_id: str,
        reason: str,
        *,
        supported_capabilities: list[str] | None = None,
        evidence_findings: list[str] | None = None,
    ) -> None:
        self._update_document(
            claim_id,
            document_id,
            {
                "status": "unusable",
                "quality_reason": reason,
                "supported_capabilities": list(supported_capabilities or []),
                "evidence_findings": list(evidence_findings or []),
            },
            "mark claim document unusable",
        )

    def mark_document_validated(
        self,
        claim_id: str,
        document_id: str,
        *,
        quality_reason: str | None = None,
        supported_capabilities: list[str] | None = None,
        evidence_findings: list[str] | None = None,
    ) -> None:
        self._update_document(
            claim_id,
            document_id,
            {
                "status": "validated",
                "quality_reason": quality_reason,
                "supported_capabilities": list(supported_capabilities or []),
                "evidence_findings": list(evidence_findings or []),
            },
            "mark claim document validated",
        )

    def mark_document_superseded(
        self, claim_id: str, document_id: str, replacement_document_id: str
    ) -> None:
        self._update_document(
            claim_id,
            document_id,
            {
                "status": "superseded",
                "superseded_by_document_id": replacement_document_id,
            },
            "mark claim document superseded",
        )

    def mark_document_resume_processed(
        self,
        claim_id: str,
        document_id: str,
        *,
        idempotency_key: str,
        result_status: str,
    ) -> None:
        self._update_document(
            claim_id,
            document_id,
            {
                "resume_idempotency_key": idempotency_key,
                "resume_processed_at": utc_now(),
                "resume_result_status": result_status,
            },
            "mark document resume processed",
        )

    def _update_document(
        self,
        claim_id: str,
        document_id: str,
        fields: Mapping[str, Any],
        action: str,
    ) -> None:
        document_ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("documents")
            .document(document_id)
        )
        try:
            document_ref.update(dict(fields))
        except Exception as exc:
            self._raise_write_error(action, exc)

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        try:
            snapshot = self._client.collection("claims").document(claim_id).get()
        except (DefaultCredentialsError, Unauthenticated, PermissionDenied) as exc:
            raise FirestoreAuthenticationError(
                "Firestore authentication failed while reading the claim."
            ) from exc
        except Exception as exc:
            raise FirestoreReadError(f"Could not read claim {claim_id}: {exc}") from exc

        if not snapshot.exists:
            return None

        return snapshot.to_dict()

    def reserve_human_review_generation(
        self,
        *,
        claim_id: str,
        generation_key: str,
        floor_generation: int = 0,
        make_current: bool = False,
    ) -> HumanReviewGeneration:
        """Transactionally map one review-producing operation to one cycle."""
        if not generation_key.strip():
            raise FirestoreWriteError("Human-review generation key is required.")
        claim_ref = self._client.collection("claims").document(claim_id)
        transaction = self._client.transaction()
        key_hash = hashlib.sha256(generation_key.encode("utf-8")).hexdigest()

        @firestore.transactional
        def reserve(transaction):
            snapshot = claim_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise FirestoreWriteError(f"Claim {claim_id} does not exist.")
            claim = snapshot.to_dict()
            generations = dict(claim.get("human_review_generations") or {})
            existing = generations.get(key_hash)
            if isinstance(existing, dict):
                generation = int(existing["generation"])
                review_id = str(existing["review_id"])
                created = False
            else:
                generation = max(
                    int(claim.get("human_review_generation") or 0),
                    floor_generation,
                ) + 1
                review_id = human_review_id(claim_id, generation)
                generations[key_hash] = {
                    "generation": generation,
                    "generation_key": generation_key,
                    "review_id": review_id,
                    "reserved_at": utc_now(),
                }
                created = True
            update: dict[str, Any] = {
                "human_review_generation": max(
                    int(claim.get("human_review_generation") or 0), generation
                ),
                "human_review_generations": generations,
            }
            if make_current:
                update.update(
                    {
                        "current_human_review_generation": generation,
                        "current_human_review_generation_key": generation_key,
                        "current_human_review_id": review_id,
                    }
                )
            transaction.update(claim_ref, update)
            return HumanReviewGeneration(
                generation=generation,
                generation_key=generation_key,
                review_id=review_id,
                created=created,
            )

        try:
            return reserve(transaction)
        except ClaimRepositoryError:
            raise
        except Exception as exc:
            self._raise_write_error("reserve human-review generation", exc)

    def set_current_human_review_generation(
        self,
        *,
        claim_id: str,
        generation: int,
        generation_key: str,
        review_id: str,
    ) -> None:
        try:
            self._client.collection("claims").document(claim_id).update(
                {
                    "human_review_generation": generation,
                    "current_human_review_generation": generation,
                    "current_human_review_generation_key": generation_key,
                    "current_human_review_id": review_id,
                }
            )
        except Exception as exc:
            self._raise_write_error("link current human-review generation", exc)

    def create_human_review(self, review: HumanReviewRecord) -> bool:
        """Create one durable review checkpoint and hash-only token index."""
        claim_ref = self._client.collection("claims").document(review.claim_id)
        review_ref = claim_ref.collection("human_reviews").document(review.review_id)
        token_ref = self._client.collection("human_review_tokens").document(
            review.token_hash
        )
        event_ref = claim_ref.collection("events").document(
            f"{review.review_id}-requested"
        )
        data = review.model_dump(mode="python")
        data["token_hash"] = review.token_hash
        data["conflict_fields"] = list(review.conflict_fields)
        batch = self._client.batch()
        try:
            batch.create(review_ref, data)
            batch.create(
                token_ref,
                {
                    "token_hash": review.token_hash,
                    "claim_id": review.claim_id,
                    "review_id": review.review_id,
                    "status": review.status,
                    "expires_at": review.expires_at,
                    "created_at": review.created_at,
                },
            )
            batch.create(
                event_ref,
                self._event_document(
                    timestamp=review.created_at,
                    action="human_review_requested",
                    actor="firstnoticeai",
                    from_status=ClaimStatus.HUMAN_REVIEW_REQUIRED.value,
                    to_status=ClaimStatus.HUMAN_REVIEW_REQUIRED.value,
                    details={
                        "review_id": review.review_id,
                        "review_generation": review.generation,
                        "reason": review.reason,
                    },
                    correlation_id=review.correlation_id,
                ),
            )
            batch.commit()
            return True
        except AlreadyExists:
            return False
        except Exception as exc:
            self._raise_write_error("create human review checkpoint", exc)

    def get_human_review(
        self, claim_id: str, review_id: str
    ) -> HumanReviewRecord | None:
        ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("human_reviews")
            .document(review_id)
        )
        try:
            snapshot = ref.get()
        except Exception as exc:
            raise FirestoreReadError(f"Could not read human review {review_id}.") from exc
        return HumanReviewRecord.model_validate(snapshot.to_dict()) if snapshot.exists else None

    def get_human_review_by_token_hash(
        self, token_hash: str
    ) -> HumanReviewRecord | None:
        try:
            index = (
                self._client.collection("human_review_tokens")
                .document(token_hash)
                .get()
            )
        except Exception as exc:
            raise FirestoreReadError("Could not validate the human review token.") from exc
        if not index.exists:
            return None
        data = index.to_dict()
        return self.get_human_review(str(data["claim_id"]), str(data["review_id"]))

    def expire_human_review(self, review: HumanReviewRecord) -> None:
        review_ref = (
            self._client.collection("claims")
            .document(review.claim_id)
            .collection("human_reviews")
            .document(review.review_id)
        )
        token_ref = self._client.collection("human_review_tokens").document(
            review.token_hash
        )
        now = utc_now()
        batch = self._client.batch()
        try:
            batch.update(review_ref, {"status": "expired"})
            batch.update(token_ref, {"status": "expired", "used_at": now})
            batch.commit()
        except Exception as exc:
            self._raise_write_error("expire human review token", exc)

    def mark_human_review_notification(
        self,
        claim_id: str,
        review_id: str,
        *,
        status: str,
        gmail_message_id: str | None = None,
        correlation_id: str,
        review_generation: int = 1,
    ) -> None:
        claim_ref = self._client.collection("claims").document(claim_id)
        review_ref = claim_ref.collection("human_reviews").document(review_id)
        fields: dict[str, Any] = {"notification_status": status}
        if gmail_message_id:
            fields["gmail_message_id"] = gmail_message_id
        batch = self._client.batch()
        try:
            batch.update(review_ref, fields)
            if status == "sent":
                batch.create(
                    claim_ref.collection("events").document(f"{review_id}-email-sent"),
                    self._event_document(
                        action="human_review_email_sent",
                        actor="gmail",
                        from_status=ClaimStatus.HUMAN_REVIEW_REQUIRED.value,
                        to_status=ClaimStatus.HUMAN_REVIEW_REQUIRED.value,
                        details={
                            "review_id": review_id,
                            "review_generation": review_generation,
                            "gmail_message_id": gmail_message_id,
                        },
                        correlation_id=correlation_id,
                    ),
                )
            batch.commit()
        except AlreadyExists:
            return
        except Exception as exc:
            self._raise_write_error("update human review notification", exc)

    def replace_human_review_token(
        self,
        review: HumanReviewRecord,
        *,
        old_token_hash: str,
        new_token_hash: str,
        expires_at: datetime,
    ) -> None:
        claim_ref = self._client.collection("claims").document(review.claim_id)
        review_ref = claim_ref.collection("human_reviews").document(review.review_id)
        old_ref = self._client.collection("human_review_tokens").document(old_token_hash)
        new_ref = self._client.collection("human_review_tokens").document(new_token_hash)
        now = utc_now()
        batch = self._client.batch()
        try:
            batch.update(old_ref, {"status": "expired", "used_at": now})
            batch.update(
                review_ref,
                {
                    "token_hash": new_token_hash,
                    "expires_at": expires_at,
                    "notification_status": "pending",
                },
            )
            batch.create(
                new_ref,
                {
                    "token_hash": new_token_hash,
                    "claim_id": review.claim_id,
                    "review_id": review.review_id,
                    "status": "pending",
                    "expires_at": expires_at,
                    "created_at": now,
                },
            )
            batch.commit()
        except Exception as exc:
            self._raise_write_error("rotate unsent human review token", exc)

    def decide_human_review(
        self,
        *,
        token_hash: str,
        decision: str,
        decision_note: str | None,
        reviewer_label: str | None,
        correction_type: str | None,
        target_document_id: str | None,
        now: datetime,
    ) -> tuple[HumanReviewRecord | None, bool]:
        """Atomically consume a review token; returns (record, duplicate)."""
        token_ref = self._client.collection("human_review_tokens").document(token_hash)
        transaction = self._client.transaction()

        @firestore.transactional
        def apply(transaction):
            token_snapshot = token_ref.get(transaction=transaction)
            if not token_snapshot.exists:
                return None, False
            token_data = token_snapshot.to_dict()
            claim_id = str(token_data["claim_id"])
            review_id = str(token_data["review_id"])
            review_ref = (
                self._client.collection("claims")
                .document(claim_id)
                .collection("human_reviews")
                .document(review_id)
            )
            review_snapshot = review_ref.get(transaction=transaction)
            if not review_snapshot.exists:
                return None, False
            current = HumanReviewRecord.model_validate(review_snapshot.to_dict())
            if current.status != "pending":
                return current, True
            if current.expires_at <= now:
                transaction.update(review_ref, {"status": "expired"})
                transaction.update(token_ref, {"status": "expired", "used_at": now})
                return current.model_copy(update={"status": "expired"}), False
            fields = {
                "status": decision,
                "decision_at": now,
                "decision_note": decision_note,
                "reviewer_label": reviewer_label,
                "correction_type": correction_type,
                "target_document_id": target_document_id,
                "decision_event_id": (
                    f"{current.claim_id}:{current.review_id}:{decision}:v1"
                ),
                "decision_publish_status": "pending",
            }
            transaction.update(review_ref, fields)
            transaction.update(token_ref, {"status": decision, "used_at": now})
            return current.model_copy(update=fields), False

        try:
            return apply(transaction)
        except Exception as exc:
            self._raise_write_error("record human review decision", exc)

    def mark_human_review_decision_published(
        self,
        claim_id: str,
        review_id: str,
        *,
        published: bool,
    ) -> None:
        ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("human_reviews")
            .document(review_id)
        )
        try:
            ref.update(
                {
                    "decision_publish_status": "published" if published else "failed",
                    "decision_published_at": utc_now() if published else None,
                }
            )
        except Exception as exc:
            self._raise_write_error("update human review publish status", exc)

    def complete_human_review_resume(
        self,
        *,
        claim_id: str,
        review_id: str,
        target_status: ClaimStatus,
        conflicts: list[dict[str, Any]],
        missing_documents: list[dict[str, Any]],
        unusable_evidence: list[dict[str, Any]],
        requested_actions: list[dict[str, Any]],
        correlation_id: str,
        approved_issue_fingerprints: list[str] | None = None,
        source_aware_conflicts: list[dict[str, Any]] | None = None,
        source_aware_uncertainties: list[dict[str, Any]] | None = None,
        unresolved_uncertainties: list[dict[str, Any]] | None = None,
    ) -> None:
        claim = self.get_claim(claim_id)
        if claim is None:
            raise FirestoreWriteError(f"Claim {claim_id} does not exist.")
        validate_claim_status_transition(
            claim.get("status", ""), ClaimStatus.REVIEW_PROCESSING
        )
        validate_claim_status_transition(ClaimStatus.REVIEW_PROCESSING, target_status)
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        review_ref = claim_ref.collection("human_reviews").document(review_id)
        event_ref = claim_ref.collection("events").document(f"{review_id}-resumed")
        try:
            batch = self._client.batch()
            claim_update: dict[str, Any] = {
                "status": target_status.value,
                "review_status": "completed",
                "requires_human_review": False,
                "human_review_reason": None,
                "conflicts": conflicts,
                "conflict_count": len(conflicts),
                "missing_documents": missing_documents,
                "missing_document_count": len(missing_documents),
                "unusable_evidence": unusable_evidence,
                "unusable_evidence_count": len(unusable_evidence),
                "requested_actions": requested_actions,
                "current_human_review_id": None,
                "current_human_review_generation_key": None,
                "intake_complete": not missing_documents and not unusable_evidence,
                "intake_priority": (
                    "expedited"
                    if claim.get("intake_priority") == "expedited"
                    else "routine"
                ),
                "updated_at": now,
            }
            if approved_issue_fingerprints is not None:
                claim_update["approved_issue_fingerprints"] = approved_issue_fingerprints
            if source_aware_conflicts is not None:
                claim_update["source_aware_conflicts"] = source_aware_conflicts
            if source_aware_uncertainties is not None:
                claim_update["source_aware_uncertainties"] = source_aware_uncertainties
            if unresolved_uncertainties is not None:
                claim_update["unresolved_uncertainties"] = unresolved_uncertainties
                claim_update["uncertainties"] = [
                    str(item.get("uncertainty"))
                    for item in unresolved_uncertainties
                    if isinstance(item, dict) and item.get("uncertainty")
                ]
            batch.update(claim_ref, claim_update)
            batch.update(
                review_ref,
                {
                    "completed_at": now,
                    "requested_actions": requested_actions,
                },
            )
            batch.create(
                event_ref,
                self._event_document(
                    timestamp=now,
                    action="human_review_resumed",
                    actor="firstnoticeai",
                    from_status=ClaimStatus.HUMAN_REVIEW_REQUIRED.value,
                    to_status=target_status.value,
                    details={
                        "review_id": review_id,
                        "review_generation": int(
                            claim.get("current_human_review_generation") or 1
                        ),
                    },
                    correlation_id=correlation_id,
                ),
            )
            batch.commit()
        except AlreadyExists:
            return
        except Exception as exc:
            self._raise_write_error("resume claim after human review", exc)

    def save_claim_correction(
        self,
        *,
        claim_id: str,
        event_id: str,
        field_name: str,
        value: str,
        correlation_id: str,
    ) -> None:
        claim_ref = self._client.collection("claims").document(claim_id)
        try:
            claim_ref.update(
                {
                    f"pending_corrections.{field_name}": value,
                    "correction_event_id": event_id,
                    "correction_correlation_id": correlation_id,
                    "updated_at": utc_now(),
                }
            )
        except Exception as exc:
            self._raise_write_error("save claimant correction", exc)

    def complete_claim_correction(
        self,
        *,
        claim_id: str,
        review_id: str,
        field_name: str,
        value: str,
        correlation_id: str,
    ) -> ClaimStatus:
        claim = self.get_claim(claim_id)
        if claim is None:
            raise FirestoreWriteError(f"Claim {claim_id} does not exist.")
        validate_claim_status_transition(
            claim.get("status", ""), ClaimStatus.REVIEW_PROCESSING
        )
        remaining_conflicts = [
            item
            for item in claim.get("conflicts", [])
            if not isinstance(item, dict) or item.get("field") != field_name
        ]
        missing = list(claim.get("missing_documents", []))
        unusable = list(claim.get("unusable_evidence", []))
        target = (
            ClaimStatus.AWAITING_DOCUMENTS
            if missing or unusable
            else ClaimStatus.INSPECTION_PENDING
        )
        validate_claim_status_transition(ClaimStatus.REVIEW_PROCESSING, target)
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        event_ref = claim_ref.collection("events").document(
            f"{review_id}-{field_name}-correction-resumed"
        )
        batch = self._client.batch()
        try:
            batch.update(
                claim_ref,
                {
                    "status": target.value,
                    field_name: value,
                    "pending_corrections": {},
                    "requested_actions": [],
                    "conflicts": remaining_conflicts,
                    "conflict_count": len(remaining_conflicts),
                    "requires_human_review": False,
                    "human_review_reason": None,
                    "intake_complete": not missing and not unusable,
                    "intake_priority": (
                        "expedited"
                        if claim.get("intake_priority") == "expedited"
                        else "routine"
                    ),
                    "updated_at": now,
                },
            )
            batch.create(
                event_ref,
                self._event_document(
                    timestamp=now,
                    action="human_review_resumed",
                    actor="firstnoticeai",
                    from_status=ClaimStatus.AWAITING_DOCUMENTS.value,
                    to_status=target.value,
                    details={"review_id": review_id, "corrected_field": field_name},
                    correlation_id=correlation_id,
                ),
            )
            batch.commit()
        except AlreadyExists:
            return ClaimStatus(str(claim.get("status")))
        except Exception as exc:
            self._raise_write_error("resume claim after claimant correction", exc)
        return target

    def create_claim_shell(
        self,
        claim_id: str,
        *,
        incident_description: str,
        policy_number_hint: str | None,
        submission_event_id: str,
        correlation_id: str,
    ) -> None:
        """Create a recoverable event-driven claim shell before file uploads."""
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        event_ref = claim_ref.collection("events").document(
            f"{submission_event_id}-submission-received"
        )
        batch = self._client.batch()
        claim = {
            "claim_id": claim_id,
            "status": ClaimStatus.NEW.value,
            "incident_description": incident_description,
            "policy_number_hint": policy_number_hint,
            "submission_event_id": submission_event_id,
            "submission_status": "uploading",
            "created_at": now,
            "updated_at": now,
            "workflow_version": WORKFLOW_VERSION,
        }
        event = self._event_document(
            timestamp=now,
            action="claim_submission_received",
            actor="claimant_api",
            from_status=None,
            to_status=ClaimStatus.NEW.value,
            details={"submission_event_id": submission_event_id},
            correlation_id=correlation_id,
        )
        try:
            batch.create(claim_ref, claim)
            batch.create(event_ref, event)
            batch.commit()
        except Exception as exc:
            self._raise_write_error("create claim shell atomically", exc)

    def create_idempotent_claim_shell(
        self,
        claim_id: str,
        *,
        idempotency_key: str,
        incident_description: str,
        policy_number_hint: str | None,
        submission_event_id: str,
        correlation_id: str,
    ) -> ClaimSubmissionReservation:
        """Atomically reserve a client request and create its claim shell.

        Firestore ``create`` preconditions make the batch safe when identical
        requests arrive concurrently. The raw client key is never persisted.
        """
        now = utc_now()
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        key_ref = self._client.collection("claim_submission_keys").document(key_hash)
        claim_ref = self._client.collection("claims").document(claim_id)
        event_ref = claim_ref.collection("events").document(
            f"{submission_event_id}-submission-received"
        )
        reservation = {
            "key_hash": key_hash,
            "claim_id": claim_id,
            "event_id": submission_event_id,
            "correlation_id": correlation_id,
            "status": "processing",
            "created_at": now,
            "updated_at": now,
        }
        claim = {
            "claim_id": claim_id,
            "status": ClaimStatus.NEW.value,
            "incident_description": incident_description,
            "policy_number_hint": policy_number_hint,
            "submission_event_id": submission_event_id,
            "submission_status": "uploading",
            "created_at": now,
            "updated_at": now,
            "workflow_version": WORKFLOW_VERSION,
        }
        event = self._event_document(
            timestamp=now,
            action="claim_submission_received",
            actor="claimant_api",
            from_status=None,
            to_status=ClaimStatus.NEW.value,
            details={"submission_event_id": submission_event_id},
            correlation_id=correlation_id,
        )
        batch = self._client.batch()
        try:
            batch.create(key_ref, reservation)
            batch.create(claim_ref, claim)
            batch.create(event_ref, event)
            batch.commit()
        except AlreadyExists:
            try:
                existing = key_ref.get()
            except Exception as exc:
                raise FirestoreReadError(
                    "Could not read an existing claim submission reservation."
                ) from exc
            if not existing.exists:
                raise DuplicateClaimError(
                    f"Claim {claim_id} already exists for another submission."
                )
            data = existing.to_dict() or {}
            return ClaimSubmissionReservation(
                claim_id=str(data["claim_id"]),
                event_id=str(data["event_id"]),
                correlation_id=str(data["correlation_id"]),
                created=False,
            )
        except Exception as exc:
            self._raise_write_error("create idempotent claim shell", exc)

        return ClaimSubmissionReservation(
            claim_id=claim_id,
            event_id=submission_event_id,
            correlation_id=correlation_id,
            created=True,
        )

    def mark_claim_submission_idempotency(
        self,
        idempotency_key: str,
        *,
        status: str,
        pubsub_message_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        fields: dict[str, Any] = {"status": status, "updated_at": utc_now()}
        if pubsub_message_id is not None:
            fields["pubsub_message_id"] = pubsub_message_id
        if error_message is not None:
            fields["error"] = error_message[:500]
        try:
            self._client.collection("claim_submission_keys").document(
                key_hash
            ).update(fields)
        except Exception as exc:
            self._raise_write_error("update claim submission reservation", exc)

    def complete_claim_shell_intake(
        self,
        claim_id: str,
        intake_result: IntakeResult,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Atomically save intake into an existing new claim shell."""
        claim = self.get_claim(claim_id)
        if claim is None:
            raise FirestoreWriteError(
                f"Could not complete claim intake: claim {claim_id} does not exist."
            )
        validate_claim_status_transition(
            claim.get("status", ""), ClaimStatus.INTAKE_COMPLETE
        )
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        event_ref = claim_ref.collection("events").document()
        batch = self._client.batch()
        update = {
            **intake_result_to_claim_fields(intake_result),
            "status": ClaimStatus.INTAKE_COMPLETE.value,
            "intake_completed_at": now,
            "updated_at": now,
        }
        event = self._event_document(
            timestamp=now,
            action="claim_intake_completed",
            actor="firstnoticeai",
            from_status=ClaimStatus.NEW.value,
            to_status=ClaimStatus.INTAKE_COMPLETE.value,
            details={"workflow_version": WORKFLOW_VERSION},
            correlation_id=correlation_id,
        )
        try:
            batch.update(claim_ref, update)
            batch.create(event_ref, event)
            batch.commit()
        except Exception as exc:
            self._raise_write_error("complete claim shell intake atomically", exc)

    def mark_claim_submission_published(
        self,
        claim_id: str,
        *,
        event_id: str,
        pubsub_message_id: str,
        correlation_id: str,
    ) -> None:
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        event_ref = claim_ref.collection("events").document(
            f"{event_id}-submission-published"
        )
        batch = self._client.batch()
        try:
            batch.update(
                claim_ref,
                {
                    "submission_status": "published",
                    "submission_published_at": now,
                    "submission_pubsub_message_id": pubsub_message_id,
                    "updated_at": now,
                },
            )
            batch.create(
                event_ref,
                self._event_document(
                    timestamp=now,
                    action="claim_submission_published",
                    actor="claimant_api",
                    from_status=ClaimStatus.NEW.value,
                    to_status=ClaimStatus.NEW.value,
                    details={
                        "event_id": event_id,
                        "pubsub_message_id": pubsub_message_id,
                    },
                    correlation_id=correlation_id,
                ),
            )
            batch.commit()
        except Exception as exc:
            self._raise_write_error("record claim submission publication", exc)

    def mark_claim_submission_publish_failed(
        self,
        claim_id: str,
        *,
        event_id: str,
        correlation_id: str,
        error_message: str,
    ) -> None:
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        event_ref = claim_ref.collection("events").document(
            f"{event_id}-submission-publish-failed"
        )
        batch = self._client.batch()
        try:
            batch.update(
                claim_ref,
                {
                    "submission_status": "publish_failed",
                    "submission_publish_error": error_message[:500],
                    "updated_at": now,
                },
            )
            batch.create(
                event_ref,
                self._event_document(
                    timestamp=now,
                    action="claim_submission_publish_failed",
                    actor="claimant_api",
                    from_status=ClaimStatus.NEW.value,
                    to_status=ClaimStatus.NEW.value,
                    details={"event_id": event_id, "error": error_message[:500]},
                    correlation_id=correlation_id,
                ),
            )
            batch.commit()
        except Exception as exc:
            self._raise_write_error("record claim submission publish failure", exc)

    def mark_claim_submission_upload_failed(
        self,
        claim_id: str,
        *,
        event_id: str,
        correlation_id: str,
        error_message: str,
    ) -> None:
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        event_ref = claim_ref.collection("events").document(
            f"{event_id}-submission-upload-failed"
        )
        batch = self._client.batch()
        try:
            batch.update(
                claim_ref,
                {
                    "submission_status": "upload_failed",
                    "submission_upload_error": error_message[:500],
                    "updated_at": now,
                },
            )
            batch.create(
                event_ref,
                self._event_document(
                    timestamp=now,
                    action="claim_submission_upload_failed",
                    actor="claimant_api",
                    from_status=ClaimStatus.NEW.value,
                    to_status=ClaimStatus.NEW.value,
                    details={"event_id": event_id, "error": error_message[:500]},
                    correlation_id=correlation_id,
                ),
            )
            batch.commit()
        except Exception as exc:
            self._raise_write_error("record claim submission upload failure", exc)

    def get_claim_events(self, claim_id: str) -> list[dict[str, Any]]:
        events_ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("events")
        )
        try:
            snapshots = events_ref.order_by("timestamp").stream()
            return [dict(snapshot.to_dict()) for snapshot in snapshots]
        except Exception as exc:
            raise FirestoreReadError(
                f"Could not read events for claim {claim_id}: {exc}"
            ) from exc

    def begin_claim_event(
        self,
        claim_id: str,
        *,
        event_id: str,
        event_type: str,
        event_version: str,
        occurred_at: datetime,
        correlation_id: str,
        source: str,
    ) -> bool:
        """Atomically reserve an event ID and record its receipt.

        Returns False when a processing/completed delivery already owns the ID.
        A retryable failed delivery may reserve the same ID for another attempt.
        """
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        processed_ref = claim_ref.collection("processed_events").document(event_id)
        timeline_ref = claim_ref.collection("events").document(
            f"{event_id}-pubsub-received"
        )
        processed = {
            "event_id": event_id,
            "event_type": event_type,
            "event_version": event_version,
            "status": "processing",
            "occurred_at": occurred_at,
            "received_at": now,
            "last_attempt_at": now,
            "attempt_count": 1,
            "correlation_id": correlation_id,
            "source": source,
        }
        timeline = self._event_document(
            timestamp=now,
            action="pubsub_event_received",
            actor="pubsub",
            from_status=None,
            to_status=None,
            details={"event_id": event_id, "event_type": event_type},
            correlation_id=correlation_id,
        )
        batch = self._client.batch()
        try:
            batch.create(processed_ref, processed)
            batch.create(timeline_ref, timeline)
            batch.commit()
            return True
        except AlreadyExists:
            try:
                snapshot = processed_ref.get()
                existing = snapshot.to_dict() if snapshot.exists else {}
            except Exception as exc:
                raise FirestoreReadError(
                    f"Could not read processed event {event_id}: {exc}"
                ) from exc

            if existing.get("status") == "failed" and existing.get("retryable"):
                try:
                    processed_ref.update(
                        {
                            "status": "processing",
                            "last_attempt_at": now,
                            "attempt_count": int(existing.get("attempt_count", 1)) + 1,
                            "error_type": firestore.DELETE_FIELD,
                            "error_message": firestore.DELETE_FIELD,
                            "failed_at": firestore.DELETE_FIELD,
                            "retryable": firestore.DELETE_FIELD,
                        }
                    )
                    return True
                except Exception as exc:
                    self._raise_write_error("retry claim event", exc)

            self.append_claim_event(
                claim_id,
                action="pubsub_event_duplicate",
                actor="pubsub",
                from_status=None,
                to_status=None,
                details={
                    "event_id": event_id,
                    "event_type": event_type,
                    "existing_status": existing.get("status", "unknown"),
                },
                correlation_id=correlation_id,
                event_id=f"{event_id}-pubsub-duplicate",
            )
            return False
        except Exception as exc:
            self._raise_write_error("begin claim event atomically", exc)

    def complete_claim_event(
        self,
        claim_id: str,
        *,
        event_id: str,
        event_type: str,
        correlation_id: str,
        result: Mapping[str, Any],
    ) -> None:
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        processed_ref = claim_ref.collection("processed_events").document(event_id)
        timeline_ref = claim_ref.collection("events").document(
            f"{event_id}-pubsub-processed"
        )
        batch = self._client.batch()
        try:
            batch.update(
                processed_ref,
                {
                    "status": "processed",
                    "processed_at": now,
                    "result": dict(result),
                },
            )
            batch.create(
                timeline_ref,
                self._event_document(
                    timestamp=now,
                    action="pubsub_event_processed",
                    actor="pubsub",
                    from_status=None,
                    to_status=None,
                    details={"event_id": event_id, "event_type": event_type},
                    correlation_id=correlation_id,
                ),
            )
            batch.commit()
        except Exception as exc:
            self._raise_write_error("complete claim event atomically", exc)

    def fail_claim_event(
        self,
        claim_id: str,
        *,
        event_id: str,
        event_type: str,
        correlation_id: str,
        error_type: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        processed_ref = claim_ref.collection("processed_events").document(event_id)
        timeline_ref = claim_ref.collection("events").document()
        batch = self._client.batch()
        failure = {
            "event_id": event_id,
            "event_type": event_type,
            "error_type": error_type,
            "error_message": error_message,
            "failed_at": now,
            "retryable": retryable,
        }
        try:
            batch.update(processed_ref, {"status": "failed", **failure})
            batch.create(
                timeline_ref,
                self._event_document(
                    timestamp=now,
                    action="pubsub_event_failed",
                    actor="pubsub",
                    from_status=None,
                    to_status=None,
                    details=failure,
                    correlation_id=correlation_id,
                ),
            )
            batch.commit()
        except Exception as exc:
            self._raise_write_error("record claim event failure atomically", exc)

    def create_appointment(self, appointment: InspectionAppointment) -> None:
        appointment_ref = (
            self._client.collection("claims")
            .document(appointment.claim_id)
            .collection("appointments")
            .document(appointment.appointment_id)
        )
        try:
            appointment_ref.create(
                appointment.model_dump(mode="python", exclude_none=True)
            )
        except AlreadyExists as exc:
            raise DuplicateAppointmentError(
                f"Appointment {appointment.appointment_id} already exists."
            ) from exc
        except Exception as exc:
            self._raise_write_error("create inspection appointment", exc)

    def get_appointment(
        self, claim_id: str, appointment_id: str
    ) -> InspectionAppointment | None:
        appointment_ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("appointments")
            .document(appointment_id)
        )
        try:
            snapshot = appointment_ref.get()
        except Exception as exc:
            raise FirestoreReadError(
                f"Could not read appointment {appointment_id}: {exc}"
            ) from exc
        if not snapshot.exists:
            return None
        return InspectionAppointment.model_validate(snapshot.to_dict())

    def get_appointments(self, claim_id: str) -> list[InspectionAppointment]:
        appointments_ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("appointments")
        )
        try:
            return [
                InspectionAppointment.model_validate(snapshot.to_dict())
                for snapshot in appointments_ref.stream()
            ]
        except Exception as exc:
            raise FirestoreReadError(
                f"Could not read appointments for claim {claim_id}: {exc}"
            ) from exc

    def get_scheduled_appointment(
        self, claim_id: str
    ) -> InspectionAppointment | None:
        return next(
            (
                appointment
                for appointment in self.get_appointments(claim_id)
                if appointment.status == "scheduled"
            ),
            None,
        )

    def mark_appointment_cancelled(
        self, claim_id: str, appointment_id: str
    ) -> None:
        appointment_ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("appointments")
            .document(appointment_id)
        )
        try:
            appointment_ref.update(
                {"status": "cancelled", "updated_at": utc_now()}
            )
        except Exception as exc:
            self._raise_write_error("cancel inspection appointment", exc)

    def schedule_inspection(
        self,
        appointment: InspectionAppointment,
        candidate_slots: list[InspectionSlot],
        *,
        correlation_id: str,
    ) -> None:
        claim = self.get_claim(appointment.claim_id)
        if claim is None:
            raise FirestoreWriteError(
                f"Could not schedule inspection: claim {appointment.claim_id} "
                "does not exist."
            )
        validate_claim_status_transition(
            claim.get("status", ""), ClaimStatus.INSPECTION_SCHEDULED
        )

        claim_ref = self._client.collection("claims").document(appointment.claim_id)
        appointment_ref = claim_ref.collection("appointments").document(
            appointment.appointment_id
        )
        slots_event_ref = claim_ref.collection("events").document(
            f"{appointment.appointment_id}-slots-generated"
        )
        scheduled_event_ref = claim_ref.collection("events").document(
            f"{appointment.appointment_id}-scheduled"
        )
        calendar_event_ref = (
            claim_ref.collection("events").document(
                f"{appointment.appointment_id}-google-calendar"
            )
            if appointment.calendar_event_id
            else None
        )
        now = utc_now()
        batch = self._client.batch()
        slots_event = self._event_document(
            timestamp=now,
            action="inspection_slots_generated",
            actor="firstnoticeai",
            from_status=ClaimStatus.INSPECTION_PENDING.value,
            to_status=ClaimStatus.INSPECTION_PENDING.value,
            details={
                "candidate_slots": [
                    slot.scheduled_start.isoformat() for slot in candidate_slots
                ]
            },
            appointment_id=appointment.appointment_id,
            correlation_id=correlation_id,
            idempotency_key=appointment.idempotency_key,
        )
        scheduled_event = self._event_document(
            timestamp=now,
            action="inspection_scheduled",
            actor="firstnoticeai",
            from_status=ClaimStatus.INSPECTION_PENDING.value,
            to_status=ClaimStatus.INSPECTION_SCHEDULED.value,
            details={
                "scheduled_start": appointment.scheduled_start.isoformat(),
                "inspection_type": appointment.inspection_type,
            },
            appointment_id=appointment.appointment_id,
            correlation_id=correlation_id,
            idempotency_key=appointment.idempotency_key,
        )
        try:
            batch.create(
                appointment_ref,
                appointment.model_dump(mode="python", exclude_none=True),
            )
            batch.update(
                claim_ref,
                {
                    "status": ClaimStatus.INSPECTION_SCHEDULED.value,
                    "scheduled_appointment_id": appointment.appointment_id,
                    "inspection_scheduled_start": appointment.scheduled_start,
                    "updated_at": now,
                },
            )
            batch.create(slots_event_ref, slots_event)
            batch.create(scheduled_event_ref, scheduled_event)
            if appointment.calendar_event_id and calendar_event_ref is not None:
                batch.create(
                    calendar_event_ref,
                    self._event_document(
                        timestamp=now,
                        action="google_calendar_event_created",
                        actor="firstnoticeai",
                        from_status=ClaimStatus.INSPECTION_PENDING.value,
                        to_status=ClaimStatus.INSPECTION_SCHEDULED.value,
                        details={
                            "appointment_id": appointment.appointment_id,
                            "calendar_event_id": appointment.calendar_event_id,
                            "scheduled_start": appointment.scheduled_start.isoformat(),
                        },
                        appointment_id=appointment.appointment_id,
                        correlation_id=correlation_id,
                        idempotency_key=appointment.idempotency_key,
                    ),
                )
            batch.commit()
        except Exception as exc:
            self._raise_write_error("schedule inspection atomically", exc)

    def create_notification(self, notification: AdjusterNotification) -> None:
        notification_ref = (
            self._client.collection("claims")
            .document(notification.claim_id)
            .collection("notifications")
            .document(notification.notification_id)
        )
        try:
            notification_ref.create(notification.model_dump(mode="python"))
        except AlreadyExists as exc:
            raise DuplicateNotificationError(
                f"Notification {notification.notification_id} already exists."
            ) from exc
        except Exception as exc:
            self._raise_write_error("create adjuster notification", exc)

    def get_notification(
        self, claim_id: str, notification_id: str
    ) -> AdjusterNotification | None:
        notification_ref = (
            self._client.collection("claims")
            .document(claim_id)
            .collection("notifications")
            .document(notification_id)
        )
        try:
            snapshot = notification_ref.get()
        except Exception as exc:
            raise FirestoreReadError(
                f"Could not read notification {notification_id}: {exc}"
            ) from exc
        if not snapshot.exists:
            return None
        return AdjusterNotification.model_validate(snapshot.to_dict())

    def complete_adjuster_dispatch(
        self,
        *,
        claim_id: str,
        appointment: InspectionAppointment,
        packet: AdjusterPacket,
        notification: AdjusterNotification,
        dispatch_idempotency_key: str,
        correlation_id: str,
    ) -> None:
        claim = self.get_claim(claim_id)
        if claim is None:
            raise FirestoreWriteError(
                f"Could not complete dispatch: claim {claim_id} does not exist."
            )
        validate_claim_status_transition(
            claim.get("status", ""), ClaimStatus.ADJUSTER_NOTIFIED
        )

        claim_ref = self._client.collection("claims").document(claim_id)
        events_ref = claim_ref.collection("events")
        now = utc_now()
        batch = self._client.batch()
        common = {
            "actor": "firstnoticeai",
            "correlation_id": correlation_id,
            "idempotency_key": dispatch_idempotency_key,
        }
        packet_event = self._event_document(
            timestamp=now,
            action="adjuster_packet_created",
            from_status=ClaimStatus.INSPECTION_SCHEDULED.value,
            to_status=ClaimStatus.INSPECTION_SCHEDULED.value,
            details={"claim_id": claim_id},
            appointment_id=appointment.appointment_id,
            **common,
        )
        notification_event = self._event_document(
            timestamp=now,
            action="adjuster_notification_sent",
            from_status=ClaimStatus.INSPECTION_SCHEDULED.value,
            to_status=ClaimStatus.INSPECTION_SCHEDULED.value,
            details={"recipient": notification.recipient},
            appointment_id=appointment.appointment_id,
            notification_id=notification.notification_id,
            **common,
        )
        moved_event = self._event_document(
            timestamp=now,
            action="claim_moved_to_adjuster_notified",
            from_status=ClaimStatus.INSPECTION_SCHEDULED.value,
            to_status=ClaimStatus.ADJUSTER_NOTIFIED.value,
            details={"reason": "Adjuster packet and notification completed."},
            appointment_id=appointment.appointment_id,
            notification_id=notification.notification_id,
            **common,
        )
        try:
            batch.update(
                claim_ref,
                {
                    "status": ClaimStatus.ADJUSTER_NOTIFIED.value,
                    "adjuster_packet": packet.model_dump(mode="python"),
                    "adjuster_notification_id": notification.notification_id,
                    "dispatch_idempotency_key": dispatch_idempotency_key,
                    "dispatch_completed_at": now,
                    "updated_at": now,
                },
            )
            batch.create(
                events_ref.document(f"{appointment.appointment_id}-packet"),
                packet_event,
            )
            batch.create(
                events_ref.document(f"{notification.notification_id}-sent"),
                notification_event,
            )
            if notification.notification_provider == "gmail":
                batch.create(
                    events_ref.document(
                        f"{notification.notification_id}-adjuster-email-sent"
                    ),
                    self._event_document(
                        timestamp=now,
                        action="adjuster_email_sent",
                        actor="gmail",
                        from_status=ClaimStatus.INSPECTION_SCHEDULED.value,
                        to_status=ClaimStatus.INSPECTION_SCHEDULED.value,
                        details={
                            "notification_id": notification.notification_id,
                            "recipient": notification.recipient,
                            "gmail_message_id": notification.gmail_message_id,
                            "appointment_id": appointment.appointment_id,
                        },
                        appointment_id=appointment.appointment_id,
                        notification_id=notification.notification_id,
                        correlation_id=correlation_id,
                        idempotency_key=dispatch_idempotency_key,
                    ),
                )
            batch.create(
                events_ref.document(f"{notification.notification_id}-claim-moved"),
                moved_event,
            )
            batch.commit()
        except Exception as exc:
            self._raise_write_error("complete adjuster dispatch atomically", exc)

    def save_completed_intake(
        self,
        intake_result: IntakeResult,
        *,
        claim_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """Atomically create an intake-complete claim and its timeline event."""
        claim_id = claim_id or generate_claim_id()
        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        event_ref = claim_ref.collection("events").document()
        batch = self._client.batch()

        claim = self._claim_document(
            claim_id=claim_id,
            intake_result=intake_result,
            status="intake_complete",
            created_at=now,
            updated_at=now,
        )
        event = self._event_document(
            timestamp=now,
            action="claim_intake_completed",
            actor="firstnoticeai",
            from_status=None,
            to_status="intake_complete",
            details={"workflow_version": WORKFLOW_VERSION},
            correlation_id=correlation_id,
        )

        try:
            # Firestore batches are atomic. create() also prevents overwriting a
            # claim if an explicit or randomly generated ID already exists.
            batch.create(claim_ref, claim)
            batch.create(event_ref, event)
            batch.commit()
        except Exception as exc:
            self._raise_write_error("save completed intake atomically", exc)

        return claim_id

    def save_review_result(
        self,
        claim_id: str,
        review_result: ReviewResult,
        *,
        correlation_id: str | None = None,
        resume_document_id: str | None = None,
        resume_idempotency_key: str | None = None,
        replacement_document: ClaimDocument | None = None,
        retry_replacement_action_id: str | None = None,
        review_generation_key: str | None = None,
    ) -> ClaimStatus:
        """Atomically persist review fields, final status, and timeline event."""
        claim = self.get_claim(claim_id)
        if claim is None:
            raise FirestoreWriteError(
                f"Could not save claim review: claim {claim_id} does not exist."
            )

        target = review_target_status(review_result)
        validate_claim_status_transition(claim.get("status", ""), target)

        generation: HumanReviewGeneration | None = None
        if target == ClaimStatus.HUMAN_REVIEW_REQUIRED:
            generation = self.reserve_human_review_generation(
                claim_id=claim_id,
                generation_key=(
                    review_generation_key
                    or f"{claim_id}:submitted-review:v1"
                ),
            )

        now = utc_now()
        claim_ref = self._client.collection("claims").document(claim_id)
        event_ref = claim_ref.collection("events").document()
        batch = self._client.batch()
        claim_update = {
            "status": target.value,
            "review_status": "completed",
            "intake_complete": review_result.intake_complete,
            "intake_priority": review_result.intake_priority,
            "inspection_required": review_result.inspection_required,
            "missing_document_count": len(review_result.missing_documents),
            "unusable_evidence_count": len(review_result.unusable_evidence),
            "conflict_count": len(review_result.conflicts),
            "requires_human_review": review_result.requires_human_review,
            "priority_reason": review_result.priority_reason,
            "review_confidence": review_result.confidence,
            "human_review_reason": review_result.human_review_reason,
            "operational_indicators": review_result.operational_indicators.model_dump(
                mode="python"
            ),
            "missing_documents": [
                item.model_dump(mode="python")
                for item in review_result.missing_documents
            ],
            "unusable_evidence": [
                item.model_dump(mode="python")
                for item in review_result.unusable_evidence
            ],
            "conflicts": [
                item.model_dump(mode="python") for item in review_result.conflicts
            ],
            "source_aware_conflicts": [
                item.model_dump(mode="python")
                for item in review_result.source_aware_conflicts
            ],
            "source_aware_uncertainties": [
                item.model_dump(mode="python")
                for item in review_result.source_aware_uncertainties
            ],
            "current_evidence_findings": [
                item.model_dump(mode="python")
                for item in review_result.current_evidence_findings
            ],
            "unresolved_uncertainties": [
                item.model_dump(mode="python")
                for item in review_result.unresolved_uncertainties
            ],
            "uncertainties": [
                item.uncertainty for item in review_result.unresolved_uncertainties
            ],
            "updated_at": now,
        }
        if generation is not None:
            claim_update.update(
                {
                    "current_human_review_generation": generation.generation,
                    "current_human_review_generation_key": generation.generation_key,
                    "current_human_review_id": generation.review_id,
                }
            )
        if replacement_document is not None:
            if (
                not replacement_document.replaces_document_id
                or not replacement_document.requested_action_id
            ):
                raise FirestoreWriteError(
                    "Atomic replacement review requires action and target IDs."
                )
            remaining_actions = [
                action.model_dump(mode="python")
                for action in parse_requested_actions(
                    claim.get("requested_actions", [])
                )
                if action.action_id != replacement_document.requested_action_id
            ]
            reservations = dict(
                claim.get("replacement_upload_reservations") or {}
            )
            reservations.pop(replacement_document.requested_action_id, None)
            claim_update.update(
                {
                    "requested_actions": remaining_actions,
                    "replacement_upload_reservations": reservations,
                }
            )
        elif retry_replacement_action_id is not None:
            reservations = dict(
                claim.get("replacement_upload_reservations") or {}
            )
            reservation = reservations.get(retry_replacement_action_id)
            if isinstance(reservation, dict):
                reservations[retry_replacement_action_id] = {
                    **reservation,
                    "status": "retry_required",
                    "updated_at": now,
                }
                claim_update["replacement_upload_reservations"] = reservations
        event = self._event_document(
            timestamp=now,
            action="claim_review_completed",
            actor="firstnoticeai",
            from_status=ClaimStatus.REVIEW_PROCESSING.value,
            to_status=target.value,
            details={
                "intake_complete": review_result.intake_complete,
                "intake_priority": review_result.intake_priority,
                "missing_documents": [
                    {"type": item.type, "reason": item.reason}
                    for item in review_result.missing_documents
                ],
                "unusable_evidence": [
                    {
                        "evidence_type": item.evidence_type,
                        "reason": item.reason,
                    }
                    for item in review_result.unusable_evidence
                ],
                "conflicts": [
                    {"field": item.field, "reason": item.reason}
                    for item in review_result.conflicts
                ],
                **(
                    {
                        "review_id": generation.review_id,
                        "review_generation": generation.generation,
                    }
                    if generation is not None
                    else {}
                ),
            },
            correlation_id=correlation_id,
        )

        try:
            batch.update(claim_ref, claim_update)
            batch.create(event_ref, event)
            if resume_document_id and resume_idempotency_key:
                document_ref = (
                    claim_ref.collection("documents").document(resume_document_id)
                )
                batch.update(
                    document_ref,
                    {
                        "resume_idempotency_key": resume_idempotency_key,
                        "resume_processed_at": now,
                        "resume_result_status": target.value,
                        **(
                            {
                                "status": "validated",
                                "quality_reason": replacement_document.quality_reason,
                                "supported_capabilities": list(
                                    replacement_document.supported_capabilities
                                ),
                                "evidence_findings": list(
                                    replacement_document.evidence_findings
                                ),
                            }
                            if replacement_document is not None
                            else {}
                        ),
                    },
                )
            if replacement_document is not None:
                replaced_ref = claim_ref.collection("documents").document(
                    replacement_document.replaces_document_id
                )
                batch.update(
                    replaced_ref,
                    {
                        "status": "superseded",
                        "superseded_by_document_id": replacement_document.document_id,
                    },
                )
            batch.commit()
        except Exception as exc:
            self._raise_write_error("save claim review atomically", exc)

        return target

    @staticmethod
    def _claim_document(
        *,
        claim_id: str,
        intake_result: IntakeResult,
        status: str,
        created_at: datetime,
        updated_at: datetime,
    ) -> dict[str, Any]:
        return {
            "claim_id": claim_id,
            "status": status,
            **intake_result_to_claim_fields(intake_result),
            "created_at": created_at,
            "updated_at": updated_at,
            "workflow_version": WORKFLOW_VERSION,
        }

    @staticmethod
    def _event_document(
        *,
        action: str,
        actor: str,
        from_status: str | None,
        to_status: str | None,
        details: Mapping[str, Any] | None,
        correlation_id: str | None,
        document_id: str | None = None,
        appointment_id: str | None = None,
        notification_id: str | None = None,
        idempotency_key: str | None = None,
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "timestamp": timestamp or utc_now(),
            "actor": actor,
            "action": action,
            "from_status": from_status,
            "to_status": to_status,
            "details": dict(details or {}),
            "document_id": document_id,
            "appointment_id": appointment_id,
            "notification_id": notification_id,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id or str(uuid4()),
        }

    @staticmethod
    def _raise_write_error(action: str, exc: Exception) -> None:
        if isinstance(exc, AlreadyExists):
            raise DuplicateClaimError(
                f"Could not {action}: the claim or event already exists."
            ) from exc
        if isinstance(
            exc, (DefaultCredentialsError, Unauthenticated, PermissionDenied)
        ):
            raise FirestoreAuthenticationError(
                f"Could not {action}: Firestore authentication or permission failed. "
                "Run 'gcloud auth application-default login' and verify the project."
            ) from exc
        if isinstance(exc, GoogleAPICallError):
            raise FirestoreWriteError(f"Could not {action}: {exc}") from exc
        raise FirestoreWriteError(f"Could not {action}: {exc}") from exc
