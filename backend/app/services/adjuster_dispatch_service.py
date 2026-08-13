from typing import Any

from google.genai import types
from pydantic import ValidationError

from app.models.adjuster_packet import (
    AdjusterNotificationDraft,
    AdjusterPacket,
)
from app.models.claim_document import ClaimDocument
from app.models.inspection_appointment import InspectionAppointment
from app.models.intake_result import IntakeResult
from app.models.review_result import ReviewResult
from app.tools.gemini_client import observed_generate_content


class AdjusterDispatchError(RuntimeError):
    """Raised when the operational adjuster handoff cannot be drafted."""


class AdjusterDispatchService:
    def __init__(
        self, client: Any, model_name: str, *, location: str = "unknown"
    ) -> None:
        self._client = client
        self._model_name = model_name
        self._location = location

    def build_packet(
        self,
        *,
        claim_id: str,
        intake_result: IntakeResult,
        review_result: ReviewResult,
        appointment: InspectionAppointment,
        documents: list[ClaimDocument],
    ) -> AdjusterPacket:
        evidence_summary = [
            f"{document.document_type}: {document.status} ({document.filename})"
            for document in documents
            if document.status != "superseded"
        ]
        unresolved_items = [
            f"Missing {item.type}: {item.reason}"
            for item in review_result.missing_documents
        ] + [
            f"Unusable {item.evidence_type}: {item.reason}"
            for item in review_result.unusable_evidence
        ]
        conflicts = [
            f"{item.field}: {item.reason}" for item in review_result.conflicts
        ]
        return AdjusterPacket(
            claim_id=claim_id,
            claim_type=intake_result.claim_type,
            incident_summary=intake_result.incident_summary,
            damage_summary=intake_result.damage_type,
            intake_priority=review_result.intake_priority,
            inspection_required=review_result.inspection_required,
            appointment_id=appointment.appointment_id,
            scheduled_inspection=appointment.scheduled_start,
            evidence_summary=evidence_summary,
            unresolved_items=unresolved_items,
            conflicts=conflicts,
            human_review_required=review_result.requires_human_review,
        )

    def draft_notification(
        self, packet: AdjusterPacket
    ) -> AdjusterNotificationDraft:
        prompt = f"""
Draft a short operational notification for an insurance adjuster using only the
validated structured packet below. Return AdjusterNotificationDraft.

Rules:
1. Summarize known facts only.
2. Clearly label unresolved items and conflicts.
3. Mention the scheduled inspection and requested operational follow-up.
4. Do not determine liability, coverage, fraud, payout, approval, or denial.
5. Do not make legal conclusions or add facts.
6. Keep the subject, message, and action requested concise.

Adjuster packet:
{packet.model_dump_json(indent=2)}
""".strip()
        try:
            response = observed_generate_content(
                self._client,
                operation="adjuster_notification_draft",
                model=self._model_name,
                location=self._location,
                claim_id=packet.claim_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=AdjusterNotificationDraft,
                ),
            )
        except Exception as exc:
            raise AdjusterDispatchError(
                f"Gemini adjuster summary generation failed: {exc}"
            ) from exc

        if not response.text:
            raise AdjusterDispatchError(
                "Gemini returned an empty adjuster summary response."
            )
        try:
            return AdjusterNotificationDraft.model_validate_json(response.text)
        except ValidationError as exc:
            raise AdjusterDispatchError(
                f"Gemini adjuster summary failed validation: {exc}"
            ) from exc
