from datetime import datetime, timezone
from uuid import uuid4

from app.domain.claim_status import ClaimStatus
from app.integrations.google_calendar_service import (
    InspectionCalendar,
    InspectionCalendarEvent,
)
from app.models.adjuster_packet import AdjusterPacket
from app.models.dispatch_result import ClaimDispatchResult
from app.models.inspection_appointment import InspectionAppointment
from app.models.intake_result import intake_result_from_claim
from app.models.notification import AdjusterNotification
from app.models.review_result import review_result_from_claim
from app.services.adjuster_dispatch_service import AdjusterDispatchService
from app.services.inspection_scheduling_service import (
    InspectionSchedulingService,
    appointment_id_for_claim,
    dispatch_idempotency_key,
    notification_id_for_claim,
)
from app.tools.firestore_repository import FirestoreClaimRepository
from app.tools.notification_tools import AdjusterNotificationTool


class ClaimDispatchWorkflowError(RuntimeError):
    """Raised when inspection scheduling and adjuster dispatch cannot proceed."""


class ClaimDispatchWorkflow:
    def __init__(
        self,
        *,
        repository: FirestoreClaimRepository,
        scheduling_service: InspectionSchedulingService,
        adjuster_service: AdjusterDispatchService,
        notification_tool: AdjusterNotificationTool,
        calendar_service: InspectionCalendar | None = None,
    ) -> None:
        self._repository = repository
        self._scheduling_service = scheduling_service
        self._adjuster_service = adjuster_service
        self._notification_tool = notification_tool
        self._calendar_service = calendar_service

    def dispatch(
        self, claim_id: str, *, now: datetime | None = None
    ) -> ClaimDispatchResult:
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        claim = self._repository.get_claim(claim_id)
        if claim is None:
            raise ClaimDispatchWorkflowError(f"Claim {claim_id} does not exist.")

        workflow_key = dispatch_idempotency_key(claim_id)
        appointment_id = appointment_id_for_claim(claim_id)
        notification_id = notification_id_for_claim(claim_id)
        previous_status = str(claim.get("status"))

        if (
            previous_status == ClaimStatus.ADJUSTER_NOTIFIED.value
            and claim.get("dispatch_idempotency_key") == workflow_key
        ):
            return self._existing_result(
                claim=claim,
                appointment_id=appointment_id,
                notification_id=notification_id,
            )

        if previous_status not in {
            ClaimStatus.INSPECTION_PENDING.value,
            ClaimStatus.INSPECTION_SCHEDULED.value,
        }:
            raise ClaimDispatchWorkflowError(
                f"Claim {claim_id} cannot dispatch from status {previous_status!r}."
            )

        intake_result = intake_result_from_claim(claim)
        review_result = review_result_from_claim(claim)
        if review_result.requires_human_review:
            raise ClaimDispatchWorkflowError(
                "Claims requiring human review cannot be scheduled automatically."
            )

        candidate_slots = []
        appointment = self._repository.get_appointment(claim_id, appointment_id)
        if previous_status == ClaimStatus.INSPECTION_PENDING.value:
            if appointment is not None:
                raise ClaimDispatchWorkflowError(
                    "An appointment exists while the claim is still inspection_pending."
                )
            candidate_slots = self._scheduling_service.generate_candidate_slots(
                now=current_time
            )
            selected_slot = self._scheduling_service.select_slot(
                candidate_slots, review_result.intake_priority
            )
            appointment = self._scheduling_service.build_appointment(
                claim=claim,
                review_result=review_result,
                slot=selected_slot,
                now=current_time,
            )
            if self._calendar_service is not None:
                calendar_event = self._calendar_service.create_inspection_event(
                    InspectionCalendarEvent(
                        appointment_id=appointment.appointment_id,
                        claim_id=claim_id,
                        scheduled_start=appointment.scheduled_start,
                        scheduled_end=appointment.scheduled_end,
                        inspection_type=appointment.inspection_type,
                        location=appointment.location_details
                        or appointment.location_type,
                        incident_summary=str(claim.get("incident_summary") or ""),
                        intake_priority=review_result.intake_priority,
                        operational_note=str(claim.get("priority_reason") or "")
                        or None,
                        inspector_name=appointment.inspector_name,
                    )
                )
                appointment = appointment.model_copy(
                    update={
                        "calendar_provider": "google_calendar",
                        "calendar_id": calendar_event.calendar_id,
                        "calendar_event_id": calendar_event.calendar_event_id,
                        "calendar_event_link": calendar_event.calendar_event_link,
                        "calendar_event_created_at": calendar_event.created_at,
                    }
                )
            self._repository.schedule_inspection(
                appointment,
                candidate_slots,
                correlation_id=str(uuid4()),
            )
        elif appointment is None:
            raise ClaimDispatchWorkflowError(
                "Claim is inspection_scheduled but its appointment is missing."
            )

        documents = self._repository.get_documents(claim_id)
        packet = self._adjuster_service.build_packet(
            claim_id=claim_id,
            intake_result=intake_result,
            review_result=review_result,
            appointment=appointment,
            documents=documents,
        )

        notification = self._repository.get_notification(claim_id, notification_id)
        if notification is None:
            draft = self._adjuster_service.draft_notification(packet)
            notification = self._notification_tool.send_adjuster_notification(
                claim_id=claim_id,
                notification_id=notification_id,
                draft=draft,
                packet=packet,
                appointment=appointment,
                idempotency_key=workflow_key,
                now=current_time,
            )

        self._repository.complete_adjuster_dispatch(
            claim_id=claim_id,
            appointment=appointment,
            packet=packet,
            notification=notification,
            dispatch_idempotency_key=workflow_key,
            correlation_id=str(uuid4()),
        )
        return ClaimDispatchResult(
            claim_id=claim_id,
            previous_status=previous_status,
            final_status=ClaimStatus.ADJUSTER_NOTIFIED.value,
            candidate_slots=candidate_slots,
            appointment=appointment,
            adjuster_packet=packet,
            notification=notification,
        )

    def _existing_result(
        self,
        *,
        claim: dict[str, object],
        appointment_id: str,
        notification_id: str,
    ) -> ClaimDispatchResult:
        claim_id = str(claim["claim_id"])
        appointment = self._repository.get_appointment(claim_id, appointment_id)
        notification = self._repository.get_notification(claim_id, notification_id)
        packet_data = claim.get("adjuster_packet")
        if appointment is None or notification is None or not isinstance(
            packet_data, dict
        ):
            raise ClaimDispatchWorkflowError(
                "Dispatch is marked complete but persisted artifacts are missing."
            )
        return ClaimDispatchResult(
            claim_id=claim_id,
            previous_status=ClaimStatus.ADJUSTER_NOTIFIED.value,
            final_status=ClaimStatus.ADJUSTER_NOTIFIED.value,
            appointment=appointment,
            adjuster_packet=AdjusterPacket.model_validate(packet_data),
            notification=AdjusterNotification.model_validate(notification),
            idempotent_replay=True,
        )
