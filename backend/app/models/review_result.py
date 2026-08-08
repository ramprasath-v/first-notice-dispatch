from typing import Literal

from pydantic import BaseModel, Field, model_validator


class UploadedEvidence(BaseModel):
    evidence_type: str = Field(description="Deterministic evidence category.")
    filename: str = Field(description="Uploaded filename; never the file contents.")
    usable: bool | None = Field(
        default=None,
        description="Known usability, or null when Gemini must assess quality.",
    )
    quality_observations: list[str] = Field(
        default_factory=list,
        description="Known quality observations supplied by the intake workflow.",
    )
    page_count: int | None = Field(default=None, ge=1)
    expected_page_count: int | None = Field(default=None, ge=1)


class ClaimEvidenceMetadata(BaseModel):
    uploaded_evidence: list[UploadedEvidence] = Field(default_factory=list)
    police_attended: bool | None = None
    vehicle_towed: bool | None = None
    vehicle_identity_clear: bool | None = None
    injury_mentioned: bool = False
    safety_concern: bool = False
    significant_damage: bool = False
    known_conflicts: list["EvidenceConflict"] = Field(default_factory=list)


class MissingEvidence(BaseModel):
    type: str = Field(description="Missing evidence category or missing component.")
    reason: str = Field(description="Why the evidence is required and missing.")
    source_requirement: str = Field(
        description="The deterministic checklist rule or uploaded evidence item."
    )


class UnusableEvidence(BaseModel):
    evidence_type: str = Field(description="Category of uploaded but unusable evidence.")
    reason: str = Field(description="Why the submitted evidence cannot be used.")
    suggested_action: str = Field(
        description="Concrete action the claimant can take to correct the problem."
    )


class EvidenceConflict(BaseModel):
    field: str = Field(description="Structured fact with conflicting values.")
    values: list[str] = Field(min_length=2)
    sources: list[str] = Field(min_length=2)
    reason: str = Field(description="Why the conflict requires resolution.")


class OperationalIndicators(BaseModel):
    possible_injury: bool = False
    safety_concern: bool = False
    significant_damage: bool = False
    high_operational_uncertainty: bool = False


class ReviewResult(BaseModel):
    intake_complete: bool
    intake_priority: Literal["routine", "expedited", "urgent_human_review"]
    priority_reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    inspection_required: bool
    missing_documents: list[MissingEvidence] = Field(default_factory=list)
    unusable_evidence: list[UnusableEvidence] = Field(default_factory=list)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    requires_human_review: bool
    human_review_reason: str | None = None
    operational_indicators: OperationalIndicators = Field(
        default_factory=OperationalIndicators,
        description="Evidence-derived indicators used by deterministic routing rules.",
    )

    @model_validator(mode="after")
    def require_human_review_reason(self) -> "ReviewResult":
        if self.requires_human_review and not self.human_review_reason:
            raise ValueError(
                "human_review_reason is required when requires_human_review is true"
            )
        return self


def review_result_from_claim(claim: dict[str, object]) -> ReviewResult:
    return ReviewResult(
        intake_complete=bool(claim.get("intake_complete", False)),
        intake_priority=str(claim.get("intake_priority", "routine")),
        priority_reason=str(
            claim.get("priority_reason", "Persisted operational routing priority.")
        ),
        confidence=float(claim.get("review_confidence", 0.0)),
        inspection_required=bool(claim.get("inspection_required", False)),
        missing_documents=[
            MissingEvidence.model_validate(item)
            for item in claim.get("missing_documents", [])
        ],
        unusable_evidence=[
            UnusableEvidence.model_validate(item)
            for item in claim.get("unusable_evidence", [])
        ],
        conflicts=[
            EvidenceConflict.model_validate(item)
            for item in claim.get("conflicts", [])
        ],
        requires_human_review=bool(claim.get("requires_human_review", False)),
        human_review_reason=claim.get("human_review_reason"),
        operational_indicators=OperationalIndicators.model_validate(
            claim.get("operational_indicators", {})
        ),
    )
