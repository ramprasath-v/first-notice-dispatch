from typing import Any

from pydantic import BaseModel

from app.models.requested_action import (
    EnterTextRequestedAction,
    RequestedAction,
    UploadDocumentRequestedAction,
)


class ClaimantActionDisplay(BaseModel):
    title: str
    explanation: str


def build_claimant_action_display(
    claim: dict[str, Any],
    requested_actions: list[RequestedAction],
    remediation_document_ids: frozenset[str] = frozenset(),
) -> ClaimantActionDisplay | None:
    """Return concise claimant copy only for grounded persisted issue facts."""

    current_action = requested_actions[0] if requested_actions else None

    if isinstance(current_action, EnterTextRequestedAction):
        fields = {
            _canonical_category(item.get("field"))
            for item in claim.get("conflicts", [])
            if isinstance(item, dict)
        }

        if (
            current_action.field_name == "policy_number"
            and "policy_number" in fields
        ):
            return ClaimantActionDisplay(
                title="Policy information doesn't match",
                explanation=(
                    "The submitted policy information contains different policy "
                    "numbers, so FirstNotice needs you to confirm the correct one."
                ),
            )

        if (
            current_action.field_name == "incident_date"
            and "incident_date" in fields
        ):
            return ClaimantActionDisplay(
                title="Incident date doesn't match",
                explanation=(
                    "The submitted evidence contains different incident dates, so "
                    "FirstNotice needs you to confirm the correct date."
                ),
            )

        return None

    if isinstance(current_action, UploadDocumentRequestedAction):
        related = _related_source_aware_issues(
            claim,
            current_action.replaces_document_id,
        )

        categories = {
            _canonical_category(item.get("field") or item.get("category"))
            for item in related
        }

        has_damage = bool(
            categories
            & {
                "damage_location",
                "vehicle_evidence_disagreement",
            }
        )

        has_identity = bool(
            categories
            & {
                "vehicle_identity",
                "vehicle_evidence_disagreement",
            }
        )

        assertions = [
            assertion
            for item in related
            for assertion in item.get("assertions", [])
            if isinstance(assertion, dict)
        ]

        report_grounded = any(
            assertion.get("document_type") == "police_report"
            or (
                assertion.get("replaceable") is False
                and str(assertion.get("filename") or "")
                .casefold()
                .endswith(".pdf")
            )
            for assertion in assertions
        ) or any(
            isinstance(item, dict)
            and str(item.get("source") or "")
            .casefold()
            .endswith(".pdf")
            for item in claim.get("current_evidence_findings", [])
        )

        has_selected_target = any(
            item.get("selected_outlier_document_id")
            == current_action.replaces_document_id
            for item in related
        )
        is_remediation_target = (
            current_action.replaces_document_id in remediation_document_ids
        )

        if has_damage and has_identity:
            return ClaimantActionDisplay(
                title=(
                    "New evidence doesn't match"
                    if is_remediation_target
                    else "Evidence doesn't match"
                ),
                explanation=(
                    "The new photo appears to show a different vehicle and damage "
                    "that does not match the collision described in the police report."
                    if is_remediation_target and report_grounded
                    else (
                        "The police report and submitted photo describe different "
                        "damage locations, and the vehicle identity could not be verified."
                        if report_grounded
                        else (
                            "The submitted evidence shows different vehicle identity "
                            "and damage information that FirstNotice cannot reconcile."
                        )
                    )
                ),
            )

        if has_damage:
            return ClaimantActionDisplay(
                title="Evidence doesn't match",
                explanation=(
                    "The police report and submitted photo describe different damage "
                    "locations."
                    if report_grounded
                    else "The submitted photos show conflicting damage locations."
                ),
            )

        if has_identity:
            if has_selected_target and is_remediation_target:
                return ClaimantActionDisplay(
                    title="This evidence doesn't match the vehicle in the claim.",
                    explanation=(
                        "The submitted photo conflicts with the vehicle identity "
                        "established by the other claim evidence."
                    ),
                )
            return ClaimantActionDisplay(
                title="Vehicle identity not verified",
                explanation=(
                    "The submitted evidence does not establish that the photos show "
                    "the same vehicle."
                ),
            )

    # Important precedence rule:
    #
    # A claimant action may still be represented through missing/requested
    # evidence rather than a typed replacement action. Before falling back to a
    # generic "license plate missing" explanation, prefer a grounded current
    # evidence disagreement when one exists.
    all_related = _related_source_aware_issues(claim, None)

    categories = {
        _canonical_category(item.get("field") or item.get("category"))
        for item in all_related
    }

    has_damage = bool(
        categories
        & {
            "damage_location",
            "vehicle_evidence_disagreement",
        }
    )

    has_identity = bool(
        categories
        & {
            "vehicle_identity",
            "vehicle_evidence_disagreement",
        }
    )

    assertions = [
        assertion
        for item in all_related
        for assertion in item.get("assertions", [])
        if isinstance(assertion, dict)
    ]

    report_grounded = any(
        assertion.get("document_type") == "police_report"
        or (
            assertion.get("replaceable") is False
            and str(assertion.get("filename") or "")
            .casefold()
            .endswith(".pdf")
        )
        for assertion in assertions
    ) or any(
        isinstance(item, dict)
        and str(item.get("source") or "")
        .casefold()
        .endswith(".pdf")
        for item in claim.get("current_evidence_findings", [])
    )

    if has_damage and has_identity:
        return ClaimantActionDisplay(
            title="New evidence doesn't match",
            explanation=(
                "The new photo does not match the damage described in the police "
                "report, and the vehicle identity still could not be verified."
                if report_grounded
                else (
                    "The submitted evidence contains conflicting vehicle identity "
                    "and damage information that FirstNotice cannot reconcile."
                )
            ),
        )

    if has_damage:
        return ClaimantActionDisplay(
            title="Evidence doesn't match",
            explanation=(
                "The new photo does not match the damage described in the police report."
                if report_grounded
                else "The submitted photos show conflicting damage locations."
            ),
        )

    missing_types = {
        str(item.get("type") or "")
        for item in claim.get("missing_documents", [])
        if isinstance(item, dict)
    }

    if missing_types & {"vehicle_identity", "license_plate_photo"}:
        return ClaimantActionDisplay(
            title="Vehicle identity not verified",
            explanation=(
                "The submitted damage photo does not show a readable license plate, "
                "so FirstNotice cannot verify the vehicle identity."
            ),
        )

    return None


def _related_source_aware_issues(
    claim: dict[str, Any],
    target_document_id: str | None,
) -> list[dict[str, Any]]:
    issues = [
        item
        for key in (
            "source_aware_conflicts",
            "source_aware_uncertainties",
        )
        for item in claim.get(key, [])
        if isinstance(item, dict)
    ]

    if target_document_id is None:
        return issues

    return [
        item
        for item in issues
        if item.get("selected_outlier_document_id") == target_document_id
        or any(
            isinstance(assertion, dict)
            and assertion.get("document_id") == target_document_id
            for assertion in item.get("assertions", [])
        )
    ]


def _canonical_category(value: object) -> str:
    normalized = "_".join(
        str(value or "")
        .casefold()
        .replace("-", " ")
        .split()
    )

    if normalized in {
        "vehicle_identity_and_damage_location",
        "damage_location_and_vehicle_identity",
        "vehicle_and_damage_mismatch",
    }:
        return "vehicle_evidence_disagreement"

    return normalized
