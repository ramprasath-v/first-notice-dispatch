import json
import hashlib
from dataclasses import asdict
from typing import Any, Sequence

from google.genai import types
from pydantic import ValidationError

from app.domain.intake_requirements import (
    IntakeRequirement,
    evaluate_intake_requirements,
)
from app.domain.evidence_reasoning import (
    canonical_active_evidence,
    fingerprint_uncertainties,
    shape_source_aware_conflicts,
    shape_source_aware_uncertainties,
)
from app.models.intake_result import IntakeResult
from app.models.gemini_review_result import GeminiReviewResult
from app.models.review_result import (
    ClaimEvidenceMetadata,
    CurrentEvidenceFinding,
    EvidenceConflict,
    MissingEvidence,
    ReviewResult,
    SourceAwareConflict,
    SourceAwareUncertainty,
    UnresolvedUncertainty,
    UnusableEvidence,
    UploadedEvidence,
)
from app.models.requested_action import (
    EnterTextRequestedAction,
    RequestedAction,
    UploadDocumentRequestedAction,
)


class ClaimReviewError(RuntimeError):
    """Raised when the evidence-quality review cannot produce a valid result."""


SIGNIFICANT_CONFLICT_FIELDS = {
    "incident_date",
    "policy_number",
    "vehicle_identity",
}
MATERIAL_OPERATIONAL_CONFLICT_FIELDS = {
    "damage_location",
    "vehicle_drivability",
}


def review_generation_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=GeminiReviewResult,
    )


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
                config=review_generation_config(),
            )
        except Exception as exc:
            raise ClaimReviewError(f"Gemini evidence review failed: {exc}") from exc

        if not response.text:
            raise ClaimReviewError("Gemini returned an empty evidence review response.")

        try:
            ai_review = GeminiReviewResult.model_validate_json(
                response.text
            ).to_domain()
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
8. Re-evaluate the complete current evidence set. Historical uncertainties in
   IntakeResult are context, not automatically current. Return only ambiguities
   that remain unresolved after considering all active evidence in
   unresolved_uncertainties.
9. Populate current_evidence_findings with concise facts and the exact submitted
   filename supporting each fact. When raw evidence parts are supplied, each is
   preceded by its Evidence source marker.
10. Persist contradictions as EvidenceConflict entries with the exact submitted
   filenames in sources. Do not treat an additional image as replacing an older
   image unless the metadata says it was superseded.
11. For every EvidenceConflict, return a matching source_aware_conflicts entry.
   Include one assertion per participating source with the conflict field, exact
   submitted filename, and concise comparable value stated by that source. Do not
   select an outlier or infer replacement eligibility; Python validates those.
12. For every unresolved uncertainty that compares two or more evidence
   artifacts, enumerate every participating submitted filename in sources. Do
   not summarize a comparison of multiple photos with only one source.
13. During resumed review, evidence-derived IntakeResult summaries may be blank
   because their original source was superseded. A blank damage summary or
   parts list is not conflicting evidence. Recompute current findings only from
   claimant-entered facts and the active submitted evidence.
14. Populate the review instrumentation fields without changing the checklist:
    - review_outcome describes whether review resolved the evidence, identified
      claimant-remediable evidence, remained ambiguous, or requires human judgment.
    - ambiguity_reason and ambiguity_summary must be null when no grounded ambiguity
      applies. Keep ambiguity_summary short and source-grounded.
    - recommended_next_step records the review recommendation only; deterministic
      Python routing remains authoritative.
15. Return only the required ReviewResult structure.

Current review context:
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
            and evidence.evidence_type not in confirmed_usable_types
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
        current_findings: list[CurrentEvidenceFinding] = []
        for evidence in metadata.uploaded_evidence:
            for finding in evidence.evidence_findings:
                candidate = CurrentEvidenceFinding(
                    source=evidence.filename, finding=finding
                )
                if candidate not in current_findings:
                    current_findings.append(candidate)
        for finding in ai_review.current_evidence_findings:
            source_key = _submitted_source_key(
                finding.source,
                filenames=submitted_filenames,
                finding_sources=submitted_finding_sources,
            )
            if source_key is None:
                continue
            candidate = finding.model_copy(update={"source": source_key})
            if candidate not in current_findings:
                current_findings.append(candidate)
        current_findings = sorted(
            current_findings,
            key=lambda item: (item.source.casefold(), item.finding.casefold()),
        )

        current_uncertainties: list[UnresolvedUncertainty] = []
        for uncertainty in ai_review.unresolved_uncertainties:
            returned_sources = list(
                dict.fromkeys(
                    " ".join(source.lower().split())
                    for source in uncertainty.sources
                    if source.strip()
                )
            )
            grounded_sources = list(
                dict.fromkeys(
                    source_key
                    for source in uncertainty.sources
                    if (
                        source_key := _submitted_source_key(
                            source,
                            filenames=submitted_filenames,
                            finding_sources=submitted_finding_sources,
                        )
                    )
                )
            )
            if not grounded_sources:
                continue
            attribution_incomplete = (
                len(grounded_sources) < len(returned_sources)
                or (
                    _describes_cross_evidence_comparison(uncertainty.uncertainty)
                    and len(grounded_sources) < 2
                )
            )
            current_uncertainties.append(
                uncertainty.model_copy(
                    update={
                        "sources": grounded_sources,
                        "source_attribution_incomplete": attribution_incomplete,
                    }
                )
            )

        source_aware = shape_source_aware_conflicts(
            conflicts,
            current_findings,
            metadata.uploaded_evidence,
            ai_review.source_aware_conflicts,
        )
        conflict_pairs = sorted(
            zip(conflicts, source_aware), key=lambda pair: pair[1].fingerprint
        )
        approved_fingerprints = set(metadata.approved_issue_fingerprints)
        conflicts = [
            conflict
            for conflict, assessment in conflict_pairs
            if assessment.fingerprint not in approved_fingerprints
        ]
        source_aware = [
            assessment
            for _, assessment in conflict_pairs
            if assessment.fingerprint not in approved_fingerprints
        ]
        fingerprinted_uncertainties = fingerprint_uncertainties(
            current_uncertainties,
            [assessment for _, assessment in conflict_pairs],
            metadata.uploaded_evidence,
        )
        source_aware_uncertainties = shape_source_aware_uncertainties(
            fingerprinted_uncertainties,
            current_findings,
            metadata.uploaded_evidence,
        )
        current_uncertainties = [
            uncertainty
            for uncertainty in fingerprinted_uncertainties
            if uncertainty.fingerprint not in approved_fingerprints
        ]
        source_aware_uncertainties = [
            assessment
            for assessment in source_aware_uncertainties
            if assessment.fingerprint not in approved_fingerprints
        ]
        current_uncertainties.sort(key=lambda item: item.fingerprint or "")
        indicators = ai_review.operational_indicators
        incident_text = intake_result.incident_summary.lower()
        explicitly_no_injury = any(
            phrase in incident_text
            for phrase in ("no injury", "no injuries", "not injured")
        )
        possible_injury = metadata.injury_mentioned or (
            indicators.possible_injury
            and not explicitly_no_injury
        )
        indicators = indicators.model_copy(
            update={"possible_injury": possible_injury}
        )
        safety_concern = metadata.safety_concern or indicators.safety_concern
        significant_damage = (
            metadata.significant_damage or indicators.significant_damage
        )
        high_uncertainty = (
            indicators.high_operational_uncertainty
            and bool(current_uncertainties)
        )
        has_resolvable_evidence_gap = bool(missing or unusable)
        has_identity_provenance_gap = any(
            item.type in {"vehicle_identity", "license_plate_photo"}
            for item in missing
        ) or any(
            item.evidence_type in {"vehicle_identity", "license_plate_photo"}
            for item in unusable
        )
        requested_actions = _reconcile_evidence_action(
            missing_documents=missing,
            unusable_evidence=unusable,
            conflicts=conflicts,
            source_aware_conflicts=source_aware,
            source_aware_uncertainties=source_aware_uncertainties,
            uploaded_evidence=metadata.uploaded_evidence,
            has_identity_provenance_gap=has_identity_provenance_gap,
        )
        grounded_safety_concern = (
            safety_concern
            and not has_identity_provenance_gap
            and not any(
                item.selected_outlier_document_id
                for item in source_aware
            )
        )
        blocking_uncertainty = (
            high_uncertainty
            and bool(current_uncertainties)
        )
        unresolved_reasoning = bool(conflicts) or blocking_uncertainty
        unsupported_discrepancy = (
            unresolved_reasoning
            and not has_resolvable_evidence_gap
            and not requested_actions
        )

        if possible_injury:
            priority = "urgent_human_review"
            priority_reason = "Possible injury requires prompt human review."
        elif grounded_safety_concern:
            priority = "urgent_human_review"
            priority_reason = "A reported safety concern requires human review."
        elif unsupported_discrepancy or (
            high_uncertainty
            and not has_resolvable_evidence_gap
            and not requested_actions
        ):
            priority = "urgent_human_review"
            priority_reason = (
                "FirstNotice could not formulate a safe claimant remediation."
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
        intake_complete = not (
            missing or unusable or conflicts or blocking_uncertainty
        )

        return ReviewResult(
            intake_complete=intake_complete,
            intake_priority=priority,
            priority_reason=priority_reason,
            confidence=ai_review.confidence,
            inspection_required=ai_review.inspection_required,
            missing_documents=missing,
            unusable_evidence=unusable,
            conflicts=conflicts,
            source_aware_conflicts=source_aware,
            source_aware_uncertainties=source_aware_uncertainties,
            current_evidence_findings=current_findings,
            unresolved_uncertainties=current_uncertainties,
            requested_actions=requested_actions,
            requires_human_review=requires_human_review,
            human_review_reason=human_review_reason,
            operational_indicators=indicators,
            review_outcome=ai_review.review_outcome,
            ambiguity_reason=ai_review.ambiguity_reason,
            recommended_next_step=ai_review.recommended_next_step,
            ambiguity_summary=ai_review.ambiguity_summary,
        )


def _reconcile_evidence_action(
    *,
    missing_documents: list[MissingEvidence],
    unusable_evidence: list[UnusableEvidence],
    conflicts: list[EvidenceConflict],
    source_aware_conflicts: list[SourceAwareConflict],
    source_aware_uncertainties: list[SourceAwareUncertainty],
    uploaded_evidence: list[UploadedEvidence],
    has_identity_provenance_gap: bool,
) -> list[RequestedAction]:
    """Reconcile current evidence into at most one safe claimant action."""
    for field_name, instruction in (
        ("policy_number", "Please confirm your policy number."),
        ("incident_date", "Please confirm the incident date."),
    ):
        conflict = next((item for item in conflicts if item.field == field_name), None)
        if conflict is not None:
            fingerprint = _action_fingerprint(field_name, conflict.sources)
            return [EnterTextRequestedAction(
                action_id=f"ACT-{fingerprint}",
                review_id=f"AUTONOMOUS-{fingerprint}",
                field_name=field_name,
                instruction=instruction,
            )]

    artifacts = canonical_active_evidence(uploaded_evidence)
    unusable_types = {item.evidence_type for item in unusable_evidence}
    unusable_targets = {
        artifact.document_id
        for artifact in artifacts
        if artifact.document_id
        and artifact.replaceable
        and artifact.status == "unusable"
        and (
            artifact.document_type in unusable_types
            or bool(set(artifact.supported_capabilities) & unusable_types)
        )
    }
    generic_action = (
        _requestable_evidence_action(missing_documents, unusable_evidence)
        if unusable_evidence and len(unusable_targets) != 1
        else (
            _requestable_evidence_action(missing_documents, [])
            if not unusable_targets
            else []
        )
    )

    source_selected = {
        item.selected_outlier_document_id
        for item in [*source_aware_conflicts, *source_aware_uncertainties]
        if item.selected_outlier_document_id
    }
    selected = unusable_targets if len(unusable_targets) == 1 else source_selected
    if len(selected) != 1:
        return generic_action
    target = next(iter(selected))
    target_artifact = next(
        (
            artifact
            for artifact in artifacts
            if artifact.document_id == target
        ),
        None,
    )
    if target_artifact is None or not target_artifact.replaceable:
        return _requestable_evidence_action(missing_documents, unusable_evidence)
    related = next(
        (
            item
            for item in [*source_aware_conflicts, *source_aware_uncertainties]
            if item.selected_outlier_document_id == target
            or any(assertion.document_id == target for assertion in item.assertions)
        ),
        None,
    )
    fingerprint = (
        related.fingerprint.removeprefix("CFP-")[:16]
        if related is not None
        else _action_fingerprint("damage_evidence", [target])
    )
    document_type = target_artifact.document_type or target_artifact.supported_capabilities[0]
    if generic_action and not _replacement_document_types_compatible(
        generic_action[0].document_type, document_type
    ):
        return generic_action
    instruction = (
        "Please upload a clear photo of the vehicle involved in this incident "
        "showing the reported damage and a readable license plate."
        if has_identity_provenance_gap
        else f"Please upload the correct {document_type.replace('_', ' ')} for this claim."
    )
    return [UploadDocumentRequestedAction(
        action_id=f"ACT-{fingerprint}",
        review_id=f"AUTONOMOUS-{fingerprint}",
        document_type=document_type,
        instruction=instruction,
        replaces_document_id=target,
    )]


def _replacement_document_types_compatible(
    requested_type: str, target_type: str
) -> bool:
    """Allow one physical vehicle image to satisfy compatible image requests."""
    if requested_type == target_type:
        return True
    image_types = {"damage_evidence", "license_plate_photo"}
    return requested_type in image_types and target_type in image_types


def _requestable_evidence_action(
    missing_documents: list[MissingEvidence],
    unusable_evidence: list[UnusableEvidence],
) -> list[RequestedAction]:
    """Return one deterministic allowlisted request for a resolvable evidence gap."""
    requests = {
        "policy_number": (
            "policy_document",
            "Please upload the policy document for this claim.",
        ),
        "policy_document": (
            "policy_document",
            "Please upload the policy document for this claim.",
        ),
        "police_report": (
            "police_report",
            "Please upload the police report for this incident.",
        ),
        "vehicle_identity": (
            "license_plate_photo",
            "Please upload a clear vehicle photo with a readable license plate.",
        ),
        "license_plate_photo": (
            "license_plate_photo",
            "Please upload a clear vehicle photo with a readable license plate.",
        ),
        "damage_evidence": (
            "damage_evidence",
            "Please upload a clear photo showing the vehicle damage.",
        ),
        "additional_damage_photo": (
            "damage_evidence",
            "Please upload a clear photo showing the vehicle damage.",
        ),
        "towing_receipt": (
            "towing_receipt",
            "Please upload the towing receipt for this incident.",
        ),
    }
    evidence_types = [
        *(item.type for item in missing_documents),
        *(item.evidence_type for item in unusable_evidence),
    ]
    candidate = next(
        (
            (evidence_type, requests[evidence_type])
            for evidence_type in evidence_types
            if evidence_type in requests
        ),
        None,
    )
    if candidate is None:
        return []
    evidence_type, (document_type, instruction) = candidate
    fingerprint = _action_fingerprint(document_type, [evidence_type])
    return [UploadDocumentRequestedAction(
        action_id=f"ACT-{fingerprint}",
        review_id=f"AUTONOMOUS-{fingerprint}",
        document_type=document_type,
        instruction=instruction,
    )]


def _single_referenced_damage_artifact(
    conflicts: list[EvidenceConflict],
    uploaded_evidence: list[UploadedEvidence],
) -> set[str]:
    """Return one auditable artifact named by a report/photo damage conflict."""
    damage_sources = {
        _normalized_source_name(source)
        for conflict in conflicts
        if conflict.field == "damage_location"
        for source in conflict.sources
    }
    if not damage_sources:
        return set()
    artifacts = canonical_active_evidence(uploaded_evidence)
    referenced = [
        artifact
        for artifact in artifacts
        if _normalized_source_name(artifact.filename) in damage_sources
    ]
    candidates = {
        artifact.document_id
        for artifact in referenced
        if artifact.replaceable and artifact.document_id
    }
    if len(candidates) != 1 or not any(
        not artifact.replaceable for artifact in referenced
    ):
        return set()
    return candidates


def _single_unusable_conflicting_artifact(
    conflicts: list[EvidenceConflict],
    uploaded_evidence: list[UploadedEvidence],
) -> set[str]:
    """Select one failed claimant artifact that introduced a current conflict."""
    conflict_sources = {
        _normalized_source_name(source)
        for conflict in conflicts
        if conflict.field in {
            "damage_location",
            "vehicle_identity",
            "vehicle_identity_and_damage_location",
        }
        for source in conflict.sources
    }
    if not conflict_sources:
        return set()
    return {
        artifact.document_id
        for artifact in canonical_active_evidence(uploaded_evidence)
        if artifact.document_id
        and artifact.replaceable
        and artifact.status == "unusable"
        and _normalized_source_name(artifact.filename) in conflict_sources
    }


def _normalized_source_name(value: str) -> str:
    return " ".join(value.casefold().replace("-", " ").split())


def _action_fingerprint(field_name: str, sources: list[str]) -> str:
    payload = f"{field_name}:{'|'.join(sorted(source.casefold() for source in sources))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16].upper()


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
    candidates = filenames | finding_sources
    if normalized in candidates:
        return normalized
    for candidate in sorted(candidates, key=lambda value: (-len(value), value)):
        if candidate in normalized:
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


def _describes_cross_evidence_comparison(uncertainty: str) -> bool:
    normalized = " ".join(uncertainty.lower().split())
    markers = (
        "two submitted",
        "two photos",
        "two images",
        "different vehicles",
        "multiple photos",
        "multiple images",
        "between the photos",
        "between the images",
        "conflicting evidence",
        "inconsistent evidence",
    )
    return any(marker in normalized for marker in markers)
