from dataclasses import dataclass

from app.models.intake_result import IntakeResult
from app.models.review_result import ClaimEvidenceMetadata, UploadedEvidence


class UnsupportedClaimTypeError(ValueError):
    """Raised when review rules do not support the extracted claim type."""


@dataclass(frozen=True)
class IntakeRequirement:
    name: str
    reason: str
    source_requirement: str
    present: bool
    usable: bool | None

    @property
    def satisfied(self) -> bool:
        return self.present and self.usable is not False


def _items(
    metadata: ClaimEvidenceMetadata, *evidence_types: str
) -> list[UploadedEvidence]:
    return [
        item
        for item in metadata.uploaded_evidence
        if item.evidence_type in evidence_types
    ]


def _evidence_requirement(
    metadata: ClaimEvidenceMetadata,
    *,
    name: str,
    reason: str,
    evidence_types: tuple[str, ...] | None = None,
) -> IntakeRequirement:
    evidence = _items(metadata, *(evidence_types or (name,)))
    usable = None
    if evidence:
        usable = not all(item.usable is False for item in evidence)

    return IntakeRequirement(
        name=name,
        reason=reason,
        source_requirement=name,
        present=bool(evidence),
        usable=usable,
    )


def evaluate_intake_requirements(
    intake_result: IntakeResult,
    metadata: ClaimEvidenceMetadata,
) -> list[IntakeRequirement]:
    """Evaluate the fixed MVP checklist for auto-collision claims."""
    if intake_result.claim_type != "auto_collision":
        raise UnsupportedClaimTypeError(
            "Review rules currently support only auto_collision claims."
        )

    vehicle_identity_items = _items(
        metadata, "vehicle_identity", "license_plate_photo"
    )
    identity_present = metadata.vehicle_identity_clear is True or bool(
        vehicle_identity_items
    )
    identity_usable = None
    if metadata.vehicle_identity_clear is False:
        identity_usable = False
    elif vehicle_identity_items:
        identity_usable = not all(item.usable is False for item in vehicle_identity_items)

    requirements = [
        IntakeRequirement(
            name="policy_number",
            reason="A policy number is required to route the claim to the policy record.",
            source_requirement="always_required",
            present=bool(intake_result.policy_number),
            usable=True if intake_result.policy_number else None,
        ),
        IntakeRequirement(
            name="incident_date",
            reason="An incident date is required for intake routing.",
            source_requirement="always_required",
            present=bool(intake_result.incident_date),
            usable=True if intake_result.incident_date else None,
        ),
        IntakeRequirement(
            name="incident_description",
            reason="A factual incident description is required for triage.",
            source_requirement="always_required",
            present=bool(intake_result.incident_summary.strip()),
            usable=True if intake_result.incident_summary.strip() else None,
        ),
        IntakeRequirement(
            name="vehicle_identity",
            reason="Vehicle identity is required to route the correct insured vehicle.",
            source_requirement="always_required",
            present=identity_present,
            usable=identity_usable,
        ),
        _evidence_requirement(
            metadata,
            name="damage_evidence",
            reason="At least one usable damage image is required for intake review.",
            evidence_types=("damage_evidence", "additional_damage_photo"),
        ),
    ]

    if metadata.police_attended is True:
        requirements.append(
            _evidence_requirement(
                metadata,
                name="police_report",
                reason="A police report is required when police attended the incident.",
            )
        )

    if metadata.vehicle_towed is True:
        requirements.append(
            _evidence_requirement(
                metadata,
                name="towing_receipt",
                reason="A towing receipt is required when the vehicle was towed.",
            )
        )

    if metadata.vehicle_identity_clear is False:
        requirements.append(
            _evidence_requirement(
                metadata,
                name="license_plate_photo",
                reason="A clear plate photo is required when vehicle identity is unclear.",
            )
        )

    damage_items = _items(metadata, "damage_evidence")
    if damage_items and all(item.usable is False for item in damage_items):
        requirements.append(
            _evidence_requirement(
                metadata,
                name="additional_damage_photo",
                reason="Another damage photo is required when current images are unusable.",
            )
        )

    return requirements
