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
