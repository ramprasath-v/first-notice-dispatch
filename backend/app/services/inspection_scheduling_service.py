from datetime import datetime, time, timedelta, timezone
from hashlib import sha256

from app.models.inspection_appointment import (
    InspectionAppointment,
    InspectionSlot,
)
from app.models.review_result import ReviewResult


class InspectionSchedulingError(RuntimeError):
    """Raised when deterministic inspection scheduling cannot proceed."""


class InspectionSchedulingService:
    WORKFLOW_VERSION = "v1"

    def generate_candidate_slots(
        self, *, now: datetime, business_days: int = 3
    ) -> list[InspectionSlot]:
        now_utc = _utc(now)
        day = now_utc.date() + timedelta(days=3)
        slots: list[InspectionSlot] = []
        included_days = 0

        while included_days < business_days:
            if day.weekday() < 5:
                for hour in (10, 14):
                    start = datetime.combine(day, time(hour=hour), tzinfo=timezone.utc)
                    slots.append(
                        InspectionSlot(
                            scheduled_start=start,
                            scheduled_end=start + timedelta(hours=1),
                        )
                    )
                included_days += 1
            day += timedelta(days=1)

        return slots

    def select_slot(
        self, slots: list[InspectionSlot], intake_priority: str
    ) -> InspectionSlot:
        if not slots:
            raise InspectionSchedulingError("No inspection slots are available.")
        if intake_priority == "urgent_human_review":
            raise InspectionSchedulingError(
                "Urgent human-review claims cannot be scheduled automatically."
            )
        if intake_priority == "expedited":
            return slots[0]
        # Routine claims use the normal afternoon window on the first business day.
        return slots[1] if len(slots) > 1 else slots[0]

    def build_appointment(
        self,
        *,
        claim: dict[str, object],
        review_result: ReviewResult,
        slot: InspectionSlot,
        now: datetime,
    ) -> InspectionAppointment:
        if review_result.requires_human_review:
            raise InspectionSchedulingError(
                "Human-review claims cannot be scheduled automatically."
            )

        claim_id = str(claim["claim_id"])
        priority = review_result.intake_priority
        vehicle_drivable = claim.get("vehicle_drivable")
        parts_affected = claim.get("parts_affected") or []
        limited_damage = isinstance(parts_affected, list) and len(parts_affected) <= 2

        if priority == "expedited" or vehicle_drivable is False:
            inspection_type = "physical"
            location_type = (
                "claimant_location"
                if vehicle_drivable is False
                else "inspection_center"
            )
            location_details = (
                "Claimant-provided vehicle location"
                if vehicle_drivable is False
                else "Demo inspection center"
            )
        elif vehicle_drivable is True and limited_damage:
            inspection_type = "virtual"
            location_type = "virtual"
            location_details = "Secure virtual inspection session"
        else:
            inspection_type = "physical"
            location_type = "inspection_center"
            location_details = "Demo inspection center"

        idempotency_key = f"{claim_id}:schedule-inspection:{self.WORKFLOW_VERSION}"
        appointment_id = _stable_id("APT", idempotency_key)
        now_utc = _utc(now)
        return InspectionAppointment(
            appointment_id=appointment_id,
            claim_id=claim_id,
            inspection_type=inspection_type,
            status="scheduled",
            scheduled_start=slot.scheduled_start,
            scheduled_end=slot.scheduled_end,
            inspector_name="Demo Inspector",
            location_type=location_type,
            location_details=location_details,
            created_at=now_utc,
            updated_at=now_utc,
            idempotency_key=idempotency_key,
        )


def dispatch_idempotency_key(claim_id: str) -> str:
    return f"{claim_id}:dispatch:v1"


def appointment_id_for_claim(claim_id: str) -> str:
    key = f"{claim_id}:schedule-inspection:v1"
    return _stable_id("APT", key)


def notification_id_for_claim(claim_id: str) -> str:
    return _stable_id("NTF", dispatch_idempotency_key(claim_id))


def _stable_id(prefix: str, value: str) -> str:
    digest = sha256(value.encode("utf-8")).hexdigest()[:8].upper()
    return f"{prefix}-{digest}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise InspectionSchedulingError("Scheduling time must be timezone-aware.")
    return value.astimezone(timezone.utc)
