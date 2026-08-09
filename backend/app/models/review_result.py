from typing import Literal

from pydantic import BaseModel, Field, model_validator


class UploadedEvidence(BaseModel):
    evidence_type: str = Field(description="Deterministic evidence category.")
    filename: str = Field(description="Uploaded filename; never the file contents.")
    document_id: str | None = Field(
        default=None, description="Internal immutable evidence identity."
    )
    source_identity: str | None = Field(
        default=None, description="Stable internal source identity."
    )
    document_type: str | None = Field(
        default=None, description="Original audit document type."
    )
    evidence_generation: str | None = None
    status: str | None = None
    usable: bool | None = Field(
        default=None,
        description="Known usability, or null when Gemini must assess quality.",
    )
    quality_observations: list[str] = Field(
        default_factory=list,
        description="Known quality observations supplied by the intake workflow.",
    )
    evidence_findings: list[str] = Field(
        default_factory=list,
        description="Current factual observations attributed to this file.",
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
    approved_issue_fingerprints: list[str] = Field(default_factory=list)


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


class ConflictSourceAssertion(BaseModel):
    field: str
    value: str
    source_identity: str
    filename: str
    document_id: str | None = None
    document_type: str | None = None
    replaceable: bool = False
    evidence_generation: str | None = None


class SourceAwareConflict(BaseModel):
    fingerprint: str
    field: str
    assertions: list[ConflictSourceAssertion] = Field(default_factory=list)
    selected_outlier_document_id: str | None = None


class SourceAwareUncertainty(BaseModel):
    fingerprint: str
    category: str
    assertions: list[ConflictSourceAssertion] = Field(default_factory=list)
    selected_outlier_document_id: str | None = None


class CurrentEvidenceFinding(BaseModel):
    source: str = Field(description="Submitted filename supporting this finding.")
    finding: str = Field(description="Current factual observation from that source.")


class UnresolvedUncertainty(BaseModel):
    uncertainty: str = Field(description="Current operational ambiguity.")
    sources: list[str] = Field(
        default_factory=list,
        description="Submitted filenames supporting or giving rise to the ambiguity.",
    )
    source_attribution_incomplete: bool = Field(
        default=False,
        description=(
            "True when a cross-evidence ambiguity could not be attributed to "
            "every artifact it compares."
        ),
    )
    fingerprint: str | None = None


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
    source_aware_conflicts: list[SourceAwareConflict] = Field(default_factory=list)
    source_aware_uncertainties: list[SourceAwareUncertainty] = Field(
        default_factory=list
    )
    current_evidence_findings: list[CurrentEvidenceFinding] = Field(
        default_factory=list
    )
    unresolved_uncertainties: list[UnresolvedUncertainty] = Field(
        default_factory=list
    )
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
        source_aware_conflicts=[
            SourceAwareConflict.model_validate(item)
            for item in claim.get("source_aware_conflicts", [])
        ],
        source_aware_uncertainties=[
            SourceAwareUncertainty.model_validate(item)
            for item in claim.get("source_aware_uncertainties", [])
        ],
        current_evidence_findings=[
            CurrentEvidenceFinding.model_validate(item)
            for item in claim.get("current_evidence_findings", [])
        ],
        unresolved_uncertainties=[
            UnresolvedUncertainty.model_validate(item)
            for item in claim.get("unresolved_uncertainties", [])
        ],
        requires_human_review=bool(claim.get("requires_human_review", False)),
        human_review_reason=claim.get("human_review_reason"),
        operational_indicators=OperationalIndicators.model_validate(
            claim.get("operational_indicators", {})
        ),
    )
