import hashlib
import json
import re
from dataclasses import dataclass

from app.models.review_result import (
    ConflictSourceAssertion,
    CurrentEvidenceFinding,
    EvidenceConflict,
    SourceAwareConflict,
    SourceAwareUncertainty,
    UnresolvedUncertainty,
    UploadedEvidence,
)


REPLACEABLE_DOCUMENT_TYPES = {
    "damage_evidence",
    "license_plate_photo",
    "policy_document",
}


@dataclass(frozen=True)
class CanonicalEvidenceArtifact:
    document_id: str | None
    source_identity: str
    filename: str
    document_type: str | None
    supported_capabilities: tuple[str, ...]
    findings: tuple[str, ...]
    status: str | None
    evidence_generation: str | None

    @property
    def replaceable(self) -> bool:
        return self.document_type in REPLACEABLE_DOCUMENT_TYPES


@dataclass(frozen=True)
class CorroboratedImageOutlier:
    document_id: str
    filename: str
    authoritative_damage_location: str
    corroborating_document_ids: tuple[str, ...]


def canonical_active_evidence(
    uploaded_evidence: list[UploadedEvidence],
) -> list[CanonicalEvidenceArtifact]:
    """Collapse per-capability metadata into stable, order-independent artifacts."""
    grouped: dict[str, dict[str, object]] = {}
    for evidence in uploaded_evidence:
        if evidence.status == "superseded":
            continue
        identity = evidence.source_identity or (
            f"document:{evidence.document_id}"
            if evidence.document_id
            else f"source:{_normalize_text(evidence.filename)}"
        )
        current = grouped.setdefault(
            identity,
            {
                "document_id": evidence.document_id,
                "filename": evidence.filename,
                "document_type": evidence.document_type or evidence.evidence_type,
                "capabilities": set(),
                "findings": set(),
                "status": evidence.status,
                "generation": evidence.evidence_generation or evidence.document_id,
            },
        )
        current["capabilities"].add(evidence.evidence_type)  # type: ignore[union-attr]
        current["findings"].update(evidence.evidence_findings)  # type: ignore[union-attr]
    return [
        CanonicalEvidenceArtifact(
            document_id=value["document_id"],  # type: ignore[arg-type]
            source_identity=identity,
            filename=str(value["filename"]),
            document_type=(str(value["document_type"]) if value["document_type"] else None),
            supported_capabilities=tuple(sorted(value["capabilities"])),  # type: ignore[arg-type]
            findings=tuple(sorted(value["findings"])),  # type: ignore[arg-type]
            status=(str(value["status"]) if value["status"] else None),
            evidence_generation=(
                str(value["generation"]) if value["generation"] else None
            ),
        )
        for identity, value in sorted(grouped.items())
    ]


def select_corroborated_image_outlier(
    conflicts: list[EvidenceConflict],
    uncertainties: list[UnresolvedUncertainty],
    findings: list[CurrentEvidenceFinding],
    uploaded_evidence: list[UploadedEvidence],
) -> CorroboratedImageOutlier | None:
    """Select one image contradicted by report-backed, identified evidence.

    This operates on normalized active-artifact facts rather than provider field
    wording. A target is safe only when a non-replaceable source and a distinct
    identity-capable image agree on damage location, while exactly one
    replaceable artifact named by the current issue disagrees.
    """
    artifacts = canonical_active_evidence(uploaded_evidence)
    findings_by_source: dict[str, list[str]] = {}
    for finding in findings:
        findings_by_source.setdefault(_normalize_text(finding.source), []).append(
            finding.finding
        )
    locations: dict[str, str] = {}
    for artifact in artifacts:
        source_findings = [
            *findings_by_source.get(_normalize_text(artifact.filename), []),
            *artifact.findings,
        ]
        location = _assertion_value("damage_location", source_findings)
        if location is not None:
            locations[artifact.source_identity] = location

    issue_sources = {
        _normalize_text(source)
        for conflict in conflicts
        if _canonical_issue_field(conflict.field) in {
            "damage_location", "vehicle_identity", "vehicle_evidence_disagreement"
        }
        for source in conflict.sources
    } | {
        _normalize_text(source)
        for uncertainty in uncertainties
        if _uncertainty_category(uncertainty.uncertainty) in {
            "damage_location", "vehicle_identity", "vehicle_evidence_disagreement"
        }
        for source in uncertainty.sources
    }
    if not issue_sources:
        return None

    candidates: dict[str, CorroboratedImageOutlier] = {}
    for authoritative in artifacts:
        authoritative_location = locations.get(authoritative.source_identity)
        if authoritative.replaceable or authoritative_location is None:
            continue
        identified_support = [
            artifact
            for artifact in artifacts
            if artifact.replaceable
            and artifact.source_identity != authoritative.source_identity
            and locations.get(artifact.source_identity) == authoritative_location
            and bool(
                {"vehicle_identity", "license_plate_photo"}
                & set(artifact.supported_capabilities)
            )
        ]
        if not identified_support:
            continue
        disagreements = [
            artifact
            for artifact in artifacts
            if artifact.replaceable
            and artifact.document_id
            and _normalize_text(artifact.filename) in issue_sources
            and locations.get(artifact.source_identity) is not None
            and locations[artifact.source_identity] != authoritative_location
        ]
        if len({item.document_id for item in disagreements}) != 1:
            continue
        candidate = disagreements[0]
        candidates[candidate.document_id] = CorroboratedImageOutlier(
            document_id=candidate.document_id,
            filename=candidate.filename,
            authoritative_damage_location=authoritative_location,
            corroborating_document_ids=tuple(
                sorted(
                    item.document_id
                    for item in identified_support
                    if item.document_id is not None
                )
            ),
        )
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def shape_source_aware_conflicts(
    conflicts: list[EvidenceConflict],
    findings: list[CurrentEvidenceFinding],
    uploaded_evidence: list[UploadedEvidence],
    review_conflicts: list[SourceAwareConflict] | None = None,
) -> list[SourceAwareConflict]:
    artifacts = canonical_active_evidence(uploaded_evidence)
    by_filename = {_normalize_text(item.filename): item for item in artifacts}
    findings_by_source: dict[str, list[str]] = {}
    for finding in findings:
        findings_by_source.setdefault(_normalize_text(finding.source), []).append(
            finding.finding
        )

    shaped: list[SourceAwareConflict] = []
    for conflict in conflicts:
        assertions: list[ConflictSourceAssertion] = []
        review_values, _ = _review_assertion_values(
            conflict, review_conflicts or []
        )
        aligned_values = (
            conflict.values if len(conflict.values) == len(conflict.sources) else []
        )
        seeded_values = review_values or {
            _normalize_text(source): value
            for source, raw_value in zip(conflict.sources, aligned_values)
            if (value := _normalize_assertion(conflict.field, raw_value)) is not None
        }
        candidate_values = set(seeded_values.values())
        for artifact in artifacts:
            source_key = _normalize_text(artifact.filename)
            source_findings = list(findings_by_source.get(source_key, []))
            source_findings.extend(artifact.findings)
            value = seeded_values.get(source_key) or _candidate_assertion_value(
                conflict.field, source_findings, candidate_values
            )
            if value is None:
                continue
            assertions.append(
                ConflictSourceAssertion(
                    field=conflict.field,
                    value=value,
                    source_identity=artifact.source_identity,
                    filename=artifact.filename,
                    document_id=artifact.document_id,
                    document_type=artifact.document_type,
                    replaceable=artifact.replaceable,
                    evidence_generation=artifact.evidence_generation,
                )
            )
        assertions = _deduplicate_assertions(assertions)
        selected_outlier = _safe_outlier(assertions)
        comparable_fields = _comparable_fields(conflict.field)
        prefer_atomic_vehicle_facts = (
            _canonical_issue_field(conflict.field) == "vehicle_identity"
        )
        if selected_outlier is None or prefer_atomic_vehicle_facts:
            comparable_selected: str | None = None
            comparable_selected_assertions: list[ConflictSourceAssertion] = []
            has_comparable_assertions = False
            conflicting_candidates = False
            for comparable_field in comparable_fields:
                comparable_assertions = _persisted_assertions(
                    comparable_field, artifacts, findings_by_source
                )
                has_comparable_assertions = (
                    has_comparable_assertions or bool(comparable_assertions)
                )
                candidate = _safe_outlier(comparable_assertions)
                if candidate is None:
                    continue
                if (
                    comparable_selected is not None
                    and comparable_selected != candidate
                ):
                    conflicting_candidates = True
                    break
                comparable_selected = candidate
                comparable_selected_assertions = comparable_assertions
            if prefer_atomic_vehicle_facts and has_comparable_assertions:
                selected_outlier = (
                    None if conflicting_candidates else comparable_selected
                )
                if comparable_selected_assertions:
                    assertions = comparable_selected_assertions
            elif selected_outlier is None and not conflicting_candidates:
                selected_outlier = comparable_selected
                if comparable_selected_assertions:
                    assertions = comparable_selected_assertions
        fingerprint = conflict_fingerprint(conflict, assertions, by_filename)
        shaped.append(
            SourceAwareConflict(
                fingerprint=fingerprint,
                field=conflict.field,
                assertions=assertions,
                selected_outlier_document_id=selected_outlier,
            )
        )
    return shaped


def _comparable_fields(field: str) -> tuple[str, ...]:
    canonical_field = _canonical_issue_field(field)
    vehicle_identity_fields = (
        "vin",
        "license_plate",
        "vehicle_make_model",
        "vehicle_year",
    )
    if canonical_field == "vehicle_identity":
        return vehicle_identity_fields
    if canonical_field == "vehicle_evidence_disagreement":
        return (*vehicle_identity_fields, "damage_location")
    return ()


def _persisted_assertions(
    field: str,
    artifacts: list[CanonicalEvidenceArtifact],
    findings_by_source: dict[str, list[str]],
) -> list[ConflictSourceAssertion]:
    assertions = []
    for artifact in artifacts:
        findings = [
            *findings_by_source.get(_normalize_text(artifact.filename), []),
            *artifact.findings,
        ]
        value = _assertion_value(field, findings)
        if value is None:
            continue
        assertions.append(ConflictSourceAssertion(
            field=field,
            value=value,
            source_identity=artifact.source_identity,
            filename=artifact.filename,
            document_id=artifact.document_id,
            document_type=artifact.document_type,
            replaceable=artifact.replaceable,
            evidence_generation=artifact.evidence_generation,
        ))
    return _deduplicate_assertions(assertions)


def _candidate_assertion_value(
    field: str,
    findings: list[str],
    candidate_values: set[str],
) -> str | None:
    """Ground one known comparable value in persisted findings, if unambiguous."""
    if not candidate_values:
        return _assertion_value(field, findings)
    comparable_findings = [_comparable_text(finding) for finding in findings]
    matches = {
        candidate
        for candidate in candidate_values
        if _comparable_text(candidate)
        and any(
            f" {_comparable_text(candidate)} " in f" {finding} "
            for finding in comparable_findings
        )
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _comparable_text(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _review_assertion_values(
    conflict: EvidenceConflict,
    review_conflicts: list[SourceAwareConflict],
) -> tuple[dict[str, str] | None, bool]:
    """Return untrusted Review values keyed by grounded source filename."""
    expected_sources = {_normalize_text(source) for source in conflict.sources}
    candidates = [
        item
        for item in review_conflicts
        if item.field == conflict.field
        and any(
            _normalize_text(assertion.filename) in expected_sources
            for assertion in item.assertions
        )
    ]
    if not candidates:
        return None, False
    candidate = max(
        candidates,
        key=lambda item: len(
            {
                _normalize_text(assertion.filename)
                for assertion in item.assertions
                if _normalize_text(assertion.filename) in expected_sources
            }
        ),
    )
    values_by_source: dict[str, set[str]] = {}
    for assertion in candidate.assertions:
        source = _normalize_text(assertion.filename)
        if assertion.field != conflict.field or source not in expected_sources:
            continue
        value = _normalize_assertion(conflict.field, assertion.value)
        if value is not None:
            values_by_source.setdefault(source, set()).add(value)
    values = {
        source: next(iter(source_values))
        for source, source_values in values_by_source.items()
        if len(source_values) == 1
    }
    return values, set(values) == expected_sources


def fingerprint_uncertainties(
    uncertainties: list[UnresolvedUncertainty],
    conflicts: list[SourceAwareConflict],
    uploaded_evidence: list[UploadedEvidence],
) -> list[UnresolvedUncertainty]:
    artifacts = canonical_active_evidence(uploaded_evidence)
    identity_by_filename = {
        _normalize_text(item.filename): item.source_identity for item in artifacts
    }
    result: list[UnresolvedUncertainty] = []
    for uncertainty in uncertainties:
        identities = sorted(
            {
                identity_by_filename.get(
                    _normalize_text(source), f"source:{_normalize_text(source)}"
                )
                for source in uncertainty.sources
            }
        )
        category = _uncertainty_category(uncertainty.uncertainty)
        related = next(
            (
                conflict
                for conflict in conflicts
                if conflict.field == category
                and identities
                and set(identities)
                == {item.source_identity for item in conflict.assertions}
            ),
            None,
        )
        fingerprint = related.fingerprint if related else _hash_payload(
            {"kind": "uncertainty", "category": category, "sources": identities}
        )
        result.append(uncertainty.model_copy(update={"fingerprint": fingerprint}))
    return result


def shape_source_aware_uncertainties(
    uncertainties: list[UnresolvedUncertainty],
    findings: list[CurrentEvidenceFinding],
    uploaded_evidence: list[UploadedEvidence],
) -> list[SourceAwareUncertainty]:
    """Select an outlier only for complete, source-grounded damage uncertainty."""
    artifacts = canonical_active_evidence(uploaded_evidence)
    by_filename = {_normalize_text(item.filename): item for item in artifacts}
    findings_by_source: dict[str, list[str]] = {}
    for finding in findings:
        findings_by_source.setdefault(_normalize_text(finding.source), []).append(
            finding.finding
        )
    assessments: list[SourceAwareUncertainty] = []
    for uncertainty in uncertainties:
        category = _uncertainty_category(uncertainty.uncertainty)
        fingerprint = uncertainty.fingerprint or _hash_payload({
            "kind": "uncertainty",
            "category": category,
            "sources": sorted(_normalize_text(source) for source in uncertainty.sources),
        })
        assertions: list[ConflictSourceAssertion] = []
        selected: str | None = None
        if (
            category == "damage_location"
            and not uncertainty.source_attribution_incomplete
            and len(set(map(_normalize_text, uncertainty.sources))) >= 2
        ):
            uncertainty_assertions = _assertions_for_sources(
                category,
                uncertainty.sources,
                findings_by_source,
                by_filename,
            )
            if len({item.value for item in uncertainty_assertions}) == 2:
                assertions = _assertions_for_sources(
                    category,
                    [item.filename for item in artifacts],
                    findings_by_source,
                    by_filename,
                )
                selected = _safe_outlier(assertions)
                uncertainty_identities = {
                    item.source_identity for item in uncertainty_assertions
                }
                selected_assertion = next(
                    (
                        item
                        for item in assertions
                        if item.document_id == selected
                    ),
                    None,
                )
                if (
                    selected_assertion is None
                    or selected_assertion.source_identity not in uncertainty_identities
                ):
                    selected = None
        assessments.append(
            SourceAwareUncertainty(
                fingerprint=fingerprint,
                category=category,
                assertions=assertions,
                selected_outlier_document_id=selected,
            )
        )
    return sorted(assessments, key=lambda item: item.fingerprint)


def conflict_fingerprint(
    conflict: EvidenceConflict,
    assertions: list[ConflictSourceAssertion],
    artifacts_by_filename: dict[str, CanonicalEvidenceArtifact] | None = None,
) -> str:
    if assertions:
        groups: dict[str, set[str]] = {}
        for assertion in assertions:
            groups.setdefault(assertion.value, set()).add(assertion.source_identity)
        payload = {
            "kind": "conflict",
            "field": conflict.field,
            "assertions": [
                {"value": value, "sources": sorted(sources)}
                for value, sources in sorted(groups.items())
            ],
        }
    else:
        artifacts_by_filename = artifacts_by_filename or {}
        payload = {
            "kind": "conflict",
            "field": conflict.field,
            "values": sorted(_normalize_text(value) for value in conflict.values),
            "sources": sorted(
                artifacts_by_filename.get(
                    _normalize_text(source),
                    CanonicalEvidenceArtifact(
                        None, f"source:{_normalize_text(source)}", source, None,
                        (), (), None, None,
                    ),
                ).source_identity
                for source in conflict.sources
            ),
        }
    return _hash_payload(payload)


def _safe_outlier(assertions: list[ConflictSourceAssertion]) -> str | None:
    groups: dict[str, list[ConflictSourceAssertion]] = {}
    for assertion in assertions:
        groups.setdefault(assertion.value, []).append(assertion)
    if len(groups) != 2:
        return None
    candidates: list[str] = []
    for value, group in groups.items():
        unique_group = {item.source_identity: item for item in group}
        if len(unique_group) != 1:
            continue
        candidate = next(iter(unique_group.values()))
        if not candidate.replaceable or not candidate.document_id:
            continue
        support = {
            item.source_identity: item
            for other_value, items in groups.items()
            if other_value != value
            for item in items
        }
        if (
            len(support) >= 2
            and any(not item.replaceable for item in support.values())
            and any(item.replaceable for item in support.values())
        ):
            candidates.append(candidate.document_id)
    return candidates[0] if len(set(candidates)) == 1 else None


def _assertion_value(field: str, findings: list[str]) -> str | None:
    if field == "vehicle_make_model":
        _, vehicle_make = _canonical_assertion_value("vehicle_make", findings)
        _, vehicle_model = _canonical_assertion_value("vehicle_model", findings)
        if vehicle_make is None or vehicle_model is None:
            return None
        return f"{vehicle_make} {vehicle_model}"

    canonical_finding_present, canonical_value = _canonical_assertion_value(
        field, findings
    )
    if field in {
        "vehicle_make",
        "vehicle_model",
        "vehicle_year",
        "license_plate",
        "vin",
    }:
        return canonical_value
    if canonical_finding_present:
        return canonical_value

    values = {
        value
        for finding in findings
        if (value := _normalize_assertion(field, finding)) is not None
    }
    return next(iter(values)) if len(values) == 1 else None


def _canonical_assertion_value(
    field: str, findings: list[str]
) -> tuple[bool, str | None]:
    canonical_values: set[str] = set()
    canonical_finding_present = False
    for finding in findings:
        finding_field, separator, raw_value = finding.partition(":")
        if not separator or finding_field.strip().casefold() != field.casefold():
            continue
        canonical_finding_present = True
        value = _normalize_assertion(field, raw_value)
        if value is not None:
            canonical_values.add(value)
    return (
        canonical_finding_present,
        next(iter(canonical_values)) if len(canonical_values) == 1 else None,
    )


def _assertions_for_sources(
    field: str,
    sources: list[str],
    findings_by_source: dict[str, list[str]],
    artifacts_by_filename: dict[str, CanonicalEvidenceArtifact],
) -> list[ConflictSourceAssertion]:
    assertions: list[ConflictSourceAssertion] = []
    for source in sources:
        source_key = _normalize_text(source)
        artifact = artifacts_by_filename.get(source_key)
        source_findings = list(findings_by_source.get(source_key, []))
        if artifact is not None:
            source_findings.extend(artifact.findings)
        value = _assertion_value(field, source_findings)
        if value is None:
            continue
        assertions.append(
            ConflictSourceAssertion(
                field=field,
                value=value,
                source_identity=(
                    artifact.source_identity
                    if artifact is not None
                    else f"source:{source_key}"
                ),
                filename=artifact.filename if artifact else source,
                document_id=artifact.document_id if artifact else None,
                document_type=artifact.document_type if artifact else None,
                replaceable=artifact.replaceable if artifact else False,
                evidence_generation=(
                    artifact.evidence_generation if artifact else None
                ),
            )
        )
    return _deduplicate_assertions(assertions)


def _normalize_assertion(field: str, value: str) -> str | None:
    normalized = _normalize_text(value)
    if field == "damage_location":
        longitudinal = {
            location
            for location, pattern in {
                "front": r"\bfront(?: end)?\b",
                "rear": r"\brear(?: end)?\b|\bfrom behind\b",
            }.items()
            if re.search(pattern, normalized)
        }
        if len(longitudinal) == 1:
            return next(iter(longitudinal))
        if longitudinal:
            return None
        lateral = {
            location
            for location, pattern in {
                "left": r"\bleft(?: side)?\b",
                "right": r"\bright(?: side)?\b",
            }.items()
            if re.search(pattern, normalized)
        }
        return next(iter(lateral)) if len(lateral) == 1 else None
    if field == "vehicle_drivability":
        if re.search(r"\bnot drivable\b|\bundrivable\b|\btowed\b", normalized):
            return "not_drivable"
        if re.search(r"\bdrivable\b", normalized):
            return "drivable"
    return normalized or None


def _uncertainty_category(value: str) -> str:
    normalized = _normalize_text(value)
    has_damage = any(
        term in normalized for term in ("front", "rear", "damage location")
    )
    has_identity = any(
        term in normalized
        for term in (
            "different vehicle",
            "same vehicle",
            "two vehicle",
            "vehicle identity",
        )
    )
    if has_damage and has_identity:
        return "vehicle_evidence_disagreement"
    if has_damage:
        return "damage_location"
    if has_identity:
        return "vehicle_identity"
    if any(term in normalized for term in ("drivable", "towed")):
        return "vehicle_drivability"
    return "operational_uncertainty"


def _canonical_issue_field(value: str) -> str:
    normalized = _normalize_text(value).replace(" ", "_")
    if normalized in {
        "vehicle_identity_and_damage_location",
        "damage_location_and_vehicle_identity",
        "vehicle_and_damage_mismatch",
    }:
        return "vehicle_evidence_disagreement"
    if normalized in {"damage_location", "vehicle_identity"}:
        return normalized
    return normalized


def _deduplicate_assertions(
    assertions: list[ConflictSourceAssertion],
) -> list[ConflictSourceAssertion]:
    unique = {
        (item.field, item.value, item.source_identity): item for item in assertions
    }
    return sorted(unique.values(), key=lambda item: (item.value, item.source_identity))


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().replace("-", " ").split())


def _hash_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "CFP-" + hashlib.sha256(encoded).hexdigest()[:20].upper()
