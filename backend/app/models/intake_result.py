from typing import Literal

from pydantic import BaseModel, Field, model_validator


EvidenceCapabilityType = Literal[
    "damage_evidence",
    "vehicle_identity",
    "license_plate_photo",
]

EvidenceArtifactType = Literal[
    "damage_evidence",
    "police_report",
    "policy_document",
    "voice_note",
    "other_evidence",
]


class EvidenceArtifactClassification(BaseModel):
    source: str = Field(description="The submitted filename being classified.")
    document_type: EvidenceArtifactType = Field(
        description="What the artifact is, based on its visible or audible content."
    )


class EvidenceArtifactFacts(BaseModel):
    source: str = Field(description="Exact submitted filename supporting these facts.")
    policy_number: str | None = None
    vehicle_identity: str | None = None
    vehicle_make: str | None = None
    vehicle_model: str | None = None
    vehicle_year: str | None = None
    license_plate: str | None = None
    vin: str | None = None
    incident_date: str | None = None
    damage_location: str | None = None

    def fact_values(self) -> dict[str, str]:
        values = self.model_dump(
            mode="python", exclude={"source"}, exclude_none=True
        )
        return {
            field_name: value.strip()
            for field_name, value in values.items()
            if value.strip()
        }

    def canonical_findings(self) -> list[str]:
        return [
            f"{field_name}: {value}"
            for field_name, value in sorted(self.fact_values().items())
        ]


class ImageEvidenceCapabilities(BaseModel):
    source: str = Field(description="The submitted image filename being assessed.")
    supported_capabilities: list[EvidenceCapabilityType] = Field(
        default_factory=list,
        description="Evidence requirements this image can visibly and usably support.",
    )
    unusable_capabilities: list[EvidenceCapabilityType] = Field(
        default_factory=list,
        description="Visible evidence that is too unclear to support the requirement.",
    )
    quality_observations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def capabilities_must_be_disjoint(self) -> "ImageEvidenceCapabilities":
        overlap = set(self.supported_capabilities) & set(self.unusable_capabilities)
        if overlap:
            raise ValueError(
                "A capability cannot be both supported and unusable: "
                + ", ".join(sorted(overlap))
            )
        return self


class EvidenceFinding(BaseModel):
    finding: str = Field(
        description="A factual observation extracted from the submitted evidence."
    )
    source: str = Field(
        description="The file that supports the observation."
    )


class IntakeResult(BaseModel):
    claim_type: Literal[
        "auto_collision",
        "weather_damage",
        "theft",
        "vandalism",
        "unknown",
    ] = Field(description="The general type of insurance claim.")

    damage_type: str = Field(
        description="A concise description of the visible or reported damage."
    )

    parts_affected: list[str] = Field(
        default_factory=list,
        description="Vehicle parts that appear damaged or are mentioned in the evidence.",
    )

    incident_summary: str = Field(
        description="A concise factual summary based only on submitted evidence."
    )

    policy_number: str | None = Field(
        default=None,
        description="Policy number found in the evidence, otherwise null.",
    )

    incident_date: str | None = Field(
        default=None,
        description="Incident date in YYYY-MM-DD format, otherwise null.",
    )

    vehicle_drivable: bool | None = Field(
        default=None,
        description="Whether the evidence explicitly indicates that the vehicle is drivable.",
    )

    evidence_findings: list[EvidenceFinding] = Field(
        default_factory=list,
        description="Important findings with the filename supporting each finding.",
    )

    evidence_artifact_classifications: list[EvidenceArtifactClassification] = Field(
        default_factory=list,
        description=(
            "Content-derived source type for each submitted artifact, independent "
            "of MIME type and image evidence capabilities."
        ),
    )

    evidence_artifact_facts: list[EvidenceArtifactFacts] = Field(
        default_factory=list,
        description=(
            "Normalized facts grouped by the exact artifact that independently "
            "supports them. Unknown or unsupported values must remain null."
        ),
    )

    image_evidence_capabilities: list[ImageEvidenceCapabilities] = Field(
        default_factory=list,
        description=(
            "Content-derived capabilities for each submitted image. One image may "
            "support multiple intake requirements."
        ),
    )

    uncertainties: list[str] = Field(
        default_factory=list,
        description="Important details that cannot be confidently determined.",
    )


def intake_result_from_claim(claim: dict[str, object]) -> IntakeResult:
    data = {
        name: claim.get(name)
        for name in (
            "claim_type",
            "damage_type",
            "parts_affected",
            "incident_summary",
            "policy_number",
            "incident_date",
            "vehicle_drivable",
            "uncertainties",
        )
    }
    if claim.get("image_evidence_capabilities") is not None:
        data["image_evidence_capabilities"] = claim[
            "image_evidence_capabilities"
        ]
    if claim.get("evidence_artifact_classifications") is not None:
        data["evidence_artifact_classifications"] = claim[
            "evidence_artifact_classifications"
        ]
    return IntakeResult.model_validate(data)
