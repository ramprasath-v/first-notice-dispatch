from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class ClaimantEvidenceRequest(BaseModel):
    document_type: str
    label: str
    instruction: str
    satisfies_requirements: list[str] = Field(default_factory=list)
    replacement_required: bool = False


@dataclass(frozen=True)
class EvidenceArtifactRule:
    document_type: str
    label: str
    instruction: str
    satisfies_requirements: frozenset[str]


LICENSE_PLATE_ARTIFACT = EvidenceArtifactRule(
    document_type="license_plate_photo",
    label="License Plate Photo",
    instruction="Please upload a clear photo of your vehicle's license plate.",
    satisfies_requirements=frozenset(
        {"vehicle_identity", "license_plate_photo"}
    ),
)


def build_claimant_evidence_requests(
    missing_documents: Iterable[dict[str, Any]],
    unusable_evidence: Iterable[dict[str, Any]] = (),
) -> list[ClaimantEvidenceRequest]:
    """Consolidate internal requirements into physical claimant artifacts."""
    unresolved: list[tuple[str, str, bool]] = []
    for item in missing_documents:
        requirement = str(item.get("type") or "").strip()
        if requirement:
            unresolved.append((requirement, str(item.get("reason") or ""), False))
    for item in unusable_evidence:
        requirement = str(item.get("evidence_type") or "").strip()
        if requirement:
            unresolved.append((requirement, str(item.get("reason") or ""), True))

    requests: list[ClaimantEvidenceRequest] = []
    unresolved_types = {requirement for requirement, _, _ in unresolved}
    plate_requirements = unresolved_types & LICENSE_PLATE_ARTIFACT.satisfies_requirements
    if plate_requirements:
        requests.append(
            ClaimantEvidenceRequest(
                document_type=LICENSE_PLATE_ARTIFACT.document_type,
                label=LICENSE_PLATE_ARTIFACT.label,
                instruction=LICENSE_PLATE_ARTIFACT.instruction,
                satisfies_requirements=sorted(
                    LICENSE_PLATE_ARTIFACT.satisfies_requirements
                ),
                replacement_required=any(
                    replacement
                    for requirement, _, replacement in unresolved
                    if requirement in plate_requirements
                ),
            )
        )

    handled = LICENSE_PLATE_ARTIFACT.satisfies_requirements
    seen: set[str] = set()
    for requirement, reason, replacement in unresolved:
        if (
            requirement in {"incident_date", "incident_description"}
            or requirement in handled
            or requirement in seen
        ):
            continue
        seen.add(requirement)
        requests.append(
            ClaimantEvidenceRequest(
                document_type=requirement,
                label=_label(requirement),
                instruction=reason
                or f"Please upload the requested {_label(requirement).lower()}.",
                satisfies_requirements=[requirement],
                replacement_required=replacement,
            )
        )
    return requests


def _label(document_type: str) -> str:
    return document_type.replace("_", " ").title()
