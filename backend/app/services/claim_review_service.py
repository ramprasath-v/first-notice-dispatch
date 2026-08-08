import json
from dataclasses import asdict
from typing import Any, Sequence

from google.genai import types
from pydantic import ValidationError

from app.domain.intake_requirements import (
    IntakeRequirement,
    evaluate_intake_requirements,
)
from app.models.intake_result import IntakeResult
from app.models.review_result import (
    ClaimEvidenceMetadata,
    EvidenceConflict,
    MissingEvidence,
    ReviewResult,
    UnusableEvidence,
)


class ClaimReviewError(RuntimeError):
    """Raised when the evidence-quality review cannot produce a valid result."""


SIGNIFICANT_CONFLICT_FIELDS = {
    "incident_date",
    "policy_number",
    "vehicle_identity",
}


class ClaimReviewService:
    def __init__(self, client: Any, model_name: str) -> None:
        self._client = client
        self._model_name = model_name

    def review(
        self,
        intake_result: IntakeResult,
        metadata: ClaimEvidenceMetadata,
        *,
        evidence_parts: Sequence[types.Part] = (),
    ) -> ReviewResult:
        requirements = evaluate_intake_requirements(intake_result, metadata)
        prompt = self._build_prompt(intake_result, metadata, requirements)

        try:
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=[
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=prompt), *evidence_parts],
                    )
                ],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=ReviewResult,
                ),
            )
        except Exception as exc:
            raise ClaimReviewError(f"Gemini evidence review failed: {exc}") from exc

        if not response.text:
            raise ClaimReviewError("Gemini returned an empty evidence review response.")

        try:
            ai_review = ReviewResult.model_validate_json(response.text)
        except ValidationError as exc:
            raise ClaimReviewError(
                f"Gemini evidence review failed ReviewResult validation: {exc}"
            ) from exc

        return self._apply_deterministic_rules(
            intake_result=intake_result,
            metadata=metadata,
            requirements=requirements,
            ai_review=ai_review,
        )

    @staticmethod
    def _build_prompt(
        intake_result: IntakeResult,
        metadata: ClaimEvidenceMetadata,
        requirements: list[IntakeRequirement],
    ) -> str:
        checklist = [asdict(requirement) for requirement in requirements]
        return f"""
You are reviewing evidence quality for first-mile insurance claim intake.

Safety and scope rules:
1. Use only the submitted evidence, validated IntakeResult, metadata, and the
   deterministic checklist below.
2. Do not invent facts or insurance document requirements.
3. The Python checklist is authoritative. Do not add general insurance rules.
4. Explicitly distinguish missing evidence, uploaded-but-unusable evidence, and
   conflicting facts.
   An omission is not a conflict unless two submitted sources state genuinely
   incompatible values.
5. A referenced but absent page may be reported as a missing component of an
   uploaded document; this is evidence completeness, not a new policy rule.
6. Do not approve or deny the claim, determine liability, decide coverage,
   conclude fraud, calculate payout, or make legal conclusions.
7. Only surface operational indicators: possible injury, safety concern,
   significant damage, or uncertainty affecting the next routing step.
8. Return only the required ReviewResult structure.

Validated IntakeResult:
{intake_result.model_dump_json(indent=2)}

Uploaded evidence metadata:
{metadata.model_dump_json(indent=2)}

Deterministic checklist evaluations:
{json.dumps(checklist, indent=2)}
""".strip()

    @staticmethod
    def _apply_deterministic_rules(
        *,
        intake_result: IntakeResult,
        metadata: ClaimEvidenceMetadata,
        requirements: list[IntakeRequirement],
        ai_review: ReviewResult,
    ) -> ReviewResult:
        missing = [
            MissingEvidence(
                type=requirement.name,
                reason=requirement.reason,
                source_requirement=requirement.source_requirement,
            )
            for requirement in requirements
            if not requirement.present
        ]

        requirement_names = {requirement.name for requirement in requirements}
        satisfied_requirement_names = {
            requirement.name for requirement in requirements if requirement.satisfied
        }
        uploaded_types = {
            evidence.evidence_type for evidence in metadata.uploaded_evidence
        }
        confirmed_usable_types = {
            evidence.evidence_type
            for evidence in metadata.uploaded_evidence
            if evidence.usable is True
        }
        for item in ai_review.missing_documents:
            if (
                item.type in confirmed_usable_types
                or item.type in satisfied_requirement_names
                or item.source_requirement in satisfied_requirement_names
            ):
                continue
            is_checklist_gap = (
                item.type in requirement_names
                or item.source_requirement in requirement_names
            )
            is_uploaded_component = any(
                item.type.startswith(f"{evidence_type}_")
                for evidence_type in uploaded_types
            )
            if is_checklist_gap or is_uploaded_component:
                _append_unique_missing(missing, item)

        unusable = [
            UnusableEvidence(
                evidence_type=evidence.evidence_type,
                reason="; ".join(evidence.quality_observations)
                or "The uploaded evidence was marked unusable.",
                suggested_action=f"Upload clearer {evidence.evidence_type} evidence.",
            )
            for evidence in metadata.uploaded_evidence
            if evidence.usable is False
        ]
        for item in ai_review.unusable_evidence:
            if (
                item.evidence_type in uploaded_types
                and item.evidence_type not in confirmed_usable_types
            ):
                _append_unique_unusable(unusable, item)

        conflicts = list(metadata.known_conflicts)
        submitted_filenames = {
            evidence.filename.lower() for evidence in metadata.uploaded_evidence
        }
        submitted_finding_sources = {
            finding.source.lower() for finding in intake_result.evidence_findings
        }
        for conflict in ai_review.conflicts:
            grounded_sources = {
                source_key
                for source in conflict.sources
                if (
                    source_key := _submitted_source_key(
                        source,
                        filenames=submitted_filenames,
                        finding_sources=submitted_finding_sources,
                    )
                )
            }
            if len(grounded_sources) < 2:
                continue
            if not any(
                existing.field == conflict.field
                and existing.values == conflict.values
                for existing in conflicts
            ):
                conflicts.append(conflict)
        significant_conflict = any(
            conflict.field in SIGNIFICANT_CONFLICT_FIELDS for conflict in conflicts
        )

        indicators = ai_review.operational_indicators
        incident_text = intake_result.incident_summary.lower()
        explicitly_no_injury = any(
            phrase in incident_text
            for phrase in ("no injury", "no injuries", "not injured")
        )
        possible_injury = metadata.injury_mentioned or (
            indicators.possible_injury
            and "injur" in incident_text
            and not explicitly_no_injury
        )
        safety_concern = metadata.safety_concern or indicators.safety_concern
        significant_damage = (
            metadata.significant_damage or indicators.significant_damage
        )
        high_uncertainty = len(intake_result.uncertainties) >= 3
        has_resolvable_evidence_gap = bool(missing or unusable)

        if possible_injury:
            priority = "urgent_human_review"
            priority_reason = "Possible injury requires prompt human review."
        elif safety_concern:
            priority = "urgent_human_review"
            priority_reason = "A reported safety concern requires human review."
        elif significant_conflict:
            priority = "urgent_human_review"
            priority_reason = "A significant factual conflict requires human review."
        elif high_uncertainty and not has_resolvable_evidence_gap:
            priority = "urgent_human_review"
            priority_reason = (
                "High uncertainty affects the next operational routing step."
            )
        elif (
            intake_result.vehicle_drivable is False
            or metadata.vehicle_towed is True
            or significant_damage
        ):
            priority = "expedited"
            priority_reason = (
                "The vehicle condition or damage requires expedited operational routing."
            )
        else:
            priority = "routine"
            priority_reason = "No urgent operational routing indicator was identified."

        requires_human_review = priority == "urgent_human_review"
        human_review_reason = priority_reason if requires_human_review else None
        intake_complete = not missing and not unusable and not significant_conflict

        return ReviewResult(
            intake_complete=intake_complete,
            intake_priority=priority,
            priority_reason=priority_reason,
            confidence=ai_review.confidence,
            inspection_required=ai_review.inspection_required,
            missing_documents=missing,
            unusable_evidence=unusable,
            conflicts=conflicts,
            requires_human_review=requires_human_review,
            human_review_reason=human_review_reason,
            operational_indicators=indicators,
        )


def _append_unique_missing(
    items: list[MissingEvidence], candidate: MissingEvidence
) -> None:
    if not any(item.type == candidate.type for item in items):
        items.append(candidate)


def _append_unique_unusable(
    items: list[UnusableEvidence], candidate: UnusableEvidence
) -> None:
    if not any(item.evidence_type == candidate.evidence_type for item in items):
        items.append(candidate)


def _submitted_source_key(
    source: str,
    *,
    filenames: set[str],
    finding_sources: set[str],
) -> str | None:
    """Return a stable key only for an actual claimant-submitted source."""
    normalized = " ".join(source.lower().split())
    workflow_markers = {
        "checklist",
        "metadata",
        "deterministic requirement",
        "workflow state",
    }
    if any(marker in normalized for marker in workflow_markers):
        return None
    for candidate in filenames | finding_sources:
        if candidate == normalized or candidate in normalized:
            return candidate
    submitted_text_sources = {
        "incident description",
        "claimant incident description",
        "claimant description",
        "claimant statement",
    }
    if normalized in submitted_text_sources:
        return normalized
    return None
