import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.api.claimant import create_claimant_app
from app.domain.claim_status import ClaimStatus
from app.events.claim_events import (
    ClaimHumanReviewApprovedEvent,
    ClaimHumanReviewCorrectionRequestedEvent,
    ClaimHumanReviewManualHandlingEvent,
)
from app.integrations.gmail_service import GmailError, GmailSendResult
from app.models.human_review import (
    HumanReviewBriefing,
    HumanReviewDecisionRequest,
    HumanReviewEvidenceRequest,
    HumanReviewDecisionResponse,
    HumanReviewPublicResponse,
    HumanReviewRecord,
    RecommendedRemediation,
    human_review_id,
)
from app.models.claim_document import ClaimDocument
from app.models.requested_action import EvidenceSourceReference
from app.models.review_result import EvidenceConflict
from app.tools.firestore_repository import HumanReviewGeneration
from app.services.human_review_service import (
    HumanReviewConflictError,
    HumanReviewExpiredError,
    HumanReviewResumeWorkflow,
    HumanReviewService,
    HumanReviewSettings,
    hash_review_token,
)


NOW = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
CLAIM_ID = "CLM-A1B2C3D4"
REVIEW_ID = "HRV-123456789ABC"


def briefing() -> HumanReviewBriefing:
    return HumanReviewBriefing(
        reason="A significant factual conflict requires human review.",
        summary="A policy-number conflict paused routing.",
        known_facts=["Claim type: auto_collision"],
        conflicts=["policy_number: POL-1001 versus POL-9999"],
        unresolved_questions=["Verify the correct value for policy_number."],
        recommended_next_action="Verify the correct value for policy_number.",
        confidence=0.91,
    )


def record(**updates) -> HumanReviewRecord:
    values = dict(
        review_id=REVIEW_ID,
        claim_id=CLAIM_ID,
        status="pending",
        reason="A significant factual conflict requires human review.",
        briefing=briefing(),
        conflict_fields=["policy_number"],
        token_hash=hash_review_token("secure-token"),
        created_at=NOW,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        correlation_id="corr-review",
    )
    values.update(updates)
    return HumanReviewRecord(**values)


class HumanReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "claim_type": "auto_collision",
            "incident_summary": "Rear impact.",
            "human_review_reason": "A significant factual conflict requires human review.",
            "review_confidence": 0.91,
            "conflicts": [
                {
                    "field": "policy_number",
                    "values": ["POL-1001", "POL-9999"],
                    "sources": ["claimant", "police-report.pdf"],
                    "reason": "Values differ.",
                }
            ],
        }
        self.repository.get_human_review.return_value = None
        self.repository.get_documents.return_value = []
        self.repository.create_human_review.return_value = True
        self.publisher = MagicMock()
        self.publisher.publish.return_value = "message-123"
        self.gmail = MagicMock()
        self.gmail.send_human_review_email.return_value = GmailSendResult(
            gmail_message_id="gmail-123",
            sent_at=NOW,
            recipient="firstnotice.adjuster@gmail.com",
            sender="sender@gmail.com",
        )
        self.service = HumanReviewService(
            repository=self.repository,
            publisher=self.publisher,
            settings=HumanReviewSettings("https://firstnotice-web.example", 60),
            gmail_sender=self.gmail,
            recipient="firstnotice.adjuster@gmail.com",
            sender="sender@gmail.com",
        )

    def test_valid_requested_incident_date_is_saved_and_published(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
            "requested_actions": [{
                "action_type": "enter_text",
                "action_id": "ACT-DATE",
                "review_id": "AUTONOMOUS-DATE",
                "field_name": "incident_date",
                "instruction": "Please provide the incident date to continue.",
            }],
        }

        self.service.submit_correction(
            CLAIM_ID, field_name="incident_date", value="2026-08-07"
        )

        self.repository.save_claim_correction.assert_called_once()
        self.assertEqual(
            self.repository.save_claim_correction.call_args.kwargs["value"],
            "2026-08-07",
        )
        self.publisher.publish.assert_called_once()

    def test_invalid_requested_incident_date_is_not_saved_or_published(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "awaiting_documents",
            "requested_actions": [{
                "action_type": "enter_text",
                "action_id": "ACT-DATE",
                "review_id": "AUTONOMOUS-DATE",
                "field_name": "incident_date",
                "instruction": "Please provide the incident date to continue.",
            }],
        }

        with self.assertRaises(HumanReviewConflictError):
            self.service.submit_correction(
                CLAIM_ID, field_name="incident_date", value="not-a-date"
            )

        self.repository.save_claim_correction.assert_not_called()
        self.publisher.publish.assert_not_called()

    def test_human_review_required_creates_one_secure_review_and_email(self) -> None:
        review = self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        created = self.repository.create_human_review.call_args.args[0]
        request = self.gmail.send_human_review_email.call_args.args[0]
        plaintext_token = request.review_url.rsplit("/", 1)[1]
        self.assertEqual(hash_review_token(plaintext_token), created.token_hash)
        self.assertNotIn(plaintext_token, created.model_dump().values())
        self.assertIn("/adjuster/review/", request.review_url)
        self.assertIn(CLAIM_ID, request.subject)
        self.assertEqual(review.notification_status, "sent")
        self.assertEqual(created.generation, 1)
        self.assertEqual(created.review_id, human_review_id(CLAIM_ID, 1))

    def test_review_link_uses_configured_canonical_web_origin(self) -> None:
        self.service._settings = HumanReviewSettings(
            "https://firstnotice-web-905025655505.us-central1.run.app", 60
        )

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        request = self.gmail.send_human_review_email.call_args.args[0]
        self.assertTrue(
            request.review_url.startswith(
                "https://firstnotice-web-905025655505.us-central1.run.app/"
                "#/adjuster/review/"
            )
        )

    def test_inspection_ready_creates_one_secure_decision_email(self) -> None:
        self.repository.get_claim.return_value.update({
            "status": "inspection_ready",
            "conflicts": [],
            "human_review_reason": None,
        })

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-ready")

        created = self.repository.create_human_review.call_args.args[0]
        request = self.gmail.send_human_review_email.call_args.args[0]
        self.assertIn("Inspection Decision Ready", request.subject)
        self.assertIn("ready for an inspection decision", created.reason)
        self.gmail.send_human_review_email.assert_called_once()

    def test_policy_conflict_recommends_enter_text(self) -> None:
        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        remediation = self.repository.create_human_review.call_args.args[
            0
        ].recommended_remediation
        self.assertEqual(remediation.type, "enter_text")
        self.assertEqual(remediation.field_name, "policy_number")
        self.assertTrue(remediation.can_request)

    def test_incident_date_conflict_recommends_enter_text(self) -> None:
        self.repository.get_claim.return_value["conflicts"] = [{
            "field": "incident_date",
            "values": ["2026-08-01", "2026-08-02"],
            "sources": ["claimant", "police-report.pdf"],
            "reason": "Dates differ.",
        }]

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        remediation = self.repository.create_human_review.call_args.args[
            0
        ].recommended_remediation
        self.assertEqual(remediation.type, "enter_text")
        self.assertEqual(remediation.field_name, "incident_date")

    def test_single_eligible_damage_source_is_selected_server_side(self) -> None:
        self.repository.get_claim.return_value["conflicts"] = [{
            "field": "damage_location",
            "values": ["rear", "front"],
            "sources": ["police-report.pdf", "damage.jpg"],
            "reason": "Damage locations differ.",
        }]
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-DAMAGE",
                claim_id=CLAIM_ID,
                document_type="damage_evidence",
                filename="damage.jpg",
                received_at=NOW,
            ),
            ClaimDocument(
                document_id="DOC-REPORT",
                claim_id=CLAIM_ID,
                document_type="police_report",
                filename="police-report.pdf",
                received_at=NOW,
            ),
        ]

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        created = self.repository.create_human_review.call_args.args[0]
        self.assertEqual(created.recommended_remediation.type, "upload_document")
        self.assertEqual(created.recommended_target_document_id, "DOC-DAMAGE")
        public = HumanReviewPublicResponse.model_validate(created.model_dump())
        public_json = public.model_dump_json()
        self.assertNotIn("document_id", public_json)
        self.assertNotIn("replacement_eligible", public_json)
        self.assertNotIn("conflict_fields", public_json)

    def test_multiple_eligible_targets_are_not_guessed(self) -> None:
        self.repository.get_claim.return_value["conflicts"] = [{
            "field": "damage_location",
            "values": ["front", "rear"],
            "sources": ["first.jpg", "second.jpg"],
            "reason": "Photos differ.",
        }]
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id=f"DOC-{index}",
                claim_id=CLAIM_ID,
                document_type="damage_evidence",
                filename=f"{name}.jpg",
                received_at=NOW,
            )
            for index, name in ((1, "first"), (2, "second"))
        ]

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        created = self.repository.create_human_review.call_args.args[0]
        self.assertFalse(created.recommended_remediation.can_request)
        self.assertIsNone(created.recommended_target_document_id)

    def test_source_aware_outlier_overrides_multiple_eligible_targets(self) -> None:
        self.repository.get_claim.return_value.update({
            "conflicts": [{
                "field": "damage_location",
                "values": ["rear", "front"],
                "sources": ["police-report.pdf", "rear.jpg", "front-plate.jpg"],
                "reason": "Damage locations differ.",
            }],
            "source_aware_conflicts": [{
                "fingerprint": "CFP-FLOW-B",
                "field": "damage_location",
                "assertions": [],
                "selected_outlier_document_id": "DOC-FRONT",
            }],
        })
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-REPORT", claim_id=CLAIM_ID,
                document_type="police_report", filename="police-report.pdf",
                received_at=NOW,
            ),
            ClaimDocument(
                document_id="DOC-REAR", claim_id=CLAIM_ID,
                document_type="damage_evidence", filename="rear.jpg",
                received_at=NOW,
            ),
            ClaimDocument(
                document_id="DOC-FRONT", claim_id=CLAIM_ID,
                document_type="license_plate_photo", filename="front-plate.jpg",
                supported_capabilities=[
                    "license_plate_photo", "vehicle_identity", "damage_evidence"
                ],
                received_at=NOW,
            ),
        ]

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-flow-b")

        created = self.repository.create_human_review.call_args.args[0]
        self.assertEqual(created.recommended_target_document_id, "DOC-FRONT")
        self.assertEqual(created.recommended_remediation.type, "upload_document")
        self.assertEqual(created.recommended_remediation.document_type, "damage_evidence")
        self.assertTrue(created.recommended_remediation.can_request)
        self.assertEqual(created.issue_fingerprints, ["CFP-FLOW-B"])

    def test_uncertainty_driven_flow_4_selects_front_plate_artifact(self) -> None:
        self.repository.get_claim.return_value.update({
            "conflicts": [],
            "unresolved_uncertainties": [{
                "uncertainty": "The front and rear vehicle relationship is unclear.",
                "sources": ["vehicle_damage.jpg", "vehicle_damage_front.jpg"],
                "source_attribution_incomplete": False,
                "fingerprint": "CFP-FLOW-4",
            }],
            "source_aware_uncertainties": [{
                "fingerprint": "CFP-FLOW-4",
                "category": "damage_location",
                "assertions": [],
                "selected_outlier_document_id": "DOC-FRONT",
            }],
        })
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-REAR", claim_id=CLAIM_ID,
                document_type="damage_evidence", filename="vehicle_damage.jpg",
                received_at=NOW,
            ),
            ClaimDocument(
                document_id="DOC-FRONT", claim_id=CLAIM_ID,
                document_type="license_plate_photo",
                filename="vehicle_damage_front.jpg", status="unusable",
                supported_capabilities=["damage_evidence", "vehicle_identity"],
                received_at=NOW,
            ),
        ]

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-flow-4")

        created = self.repository.create_human_review.call_args.args[0]
        self.assertEqual(created.recommended_target_document_id, "DOC-FRONT")
        self.assertTrue(created.recommended_remediation.can_request)
        self.assertEqual(created.recommended_remediation.document_type, "damage_evidence")

    def test_new_durable_generation_creates_independent_cycle_two(self) -> None:
        self.repository.get_claim.return_value.update(
            {
                "human_review_generation": 2,
                "current_human_review_generation": 2,
                "current_human_review_generation_key": f"{CLAIM_ID}:DOC-2:resume",
                "current_human_review_id": human_review_id(CLAIM_ID, 2),
                "conflicts": [{
                    "field": "incident_date",
                    "values": ["2026-08-01", "2026-08-02"],
                    "sources": ["claimant", "police-report.pdf"],
                    "reason": "Dates differ in the current review cycle.",
                }],
            }
        )

        review = self.service.ensure_review_requested(
            CLAIM_ID, correlation_id="corr-cycle-2"
        )

        created = self.repository.create_human_review.call_args.args[0]
        token = self.gmail.send_human_review_email.call_args.args[0].review_url.rsplit(
            "/", 1
        )[1]
        self.assertEqual(review.review_id, human_review_id(CLAIM_ID, 2))
        self.assertNotEqual(review.review_id, human_review_id(CLAIM_ID, 1))
        self.assertEqual(created.generation, 2)
        self.assertEqual(created.generation_key, f"{CLAIM_ID}:DOC-2:resume")
        self.assertEqual(created.recommended_remediation.field_name, "incident_date")
        self.assertNotEqual(hash_review_token(token), hash_review_token("secure-token"))

    def test_third_durable_generation_creates_cycle_three(self) -> None:
        self.repository.get_claim.return_value.update(
            {
                "human_review_generation": 3,
                "current_human_review_generation": 3,
                "current_human_review_generation_key": f"{CLAIM_ID}:DOC-3:resume",
                "current_human_review_id": human_review_id(CLAIM_ID, 3),
            }
        )

        review = self.service.ensure_review_requested(
            CLAIM_ID, correlation_id="corr-cycle-3"
        )

        self.assertEqual(review.review_id, human_review_id(CLAIM_ID, 3))
        self.assertEqual(
            self.repository.create_human_review.call_args.args[0].generation, 3
        )

    def test_legacy_stuck_reentry_reserves_cycle_two_from_durable_timeline(self) -> None:
        cycle_one_id = human_review_id(CLAIM_ID, 1)
        cycle_one = record(
            review_id=cycle_one_id,
            status="correction_requested",
            notification_status="sent",
        )
        self.repository.get_human_review.side_effect = [cycle_one, None]
        self.repository.get_claim_events.return_value = [
            {
                "timestamp": NOW,
                "action": "human_review_resumed",
                "details": {"review_id": cycle_one_id},
            },
            {
                "timestamp": NOW + timedelta(minutes=1),
                "action": "claim_moved_to_human_review",
                "to_status": "human_review_required",
                "details": {},
                "document_id": "DOC-REPLACEMENT",
                "correlation_id": "corr-reentry",
            },
        ]
        self.repository.reserve_human_review_generation.return_value = (
            HumanReviewGeneration(
                generation=2,
                generation_key=f"{CLAIM_ID}:DOC-REPLACEMENT:resume",
                review_id=human_review_id(CLAIM_ID, 2),
                created=True,
            )
        )

        review = self.service.ensure_review_requested(
            CLAIM_ID, correlation_id="corr-reentry"
        )

        self.assertEqual(review.generation, 2)
        self.repository.reserve_human_review_generation.assert_called_once_with(
            claim_id=CLAIM_ID,
            generation_key=f"{CLAIM_ID}:DOC-REPLACEMENT:resume",
            floor_generation=1,
            make_current=True,
        )
        self.gmail.send_human_review_email.assert_called_once()

    def test_briefing_uses_current_source_attributed_evidence(self) -> None:
        self.repository.get_claim.return_value.update(
            {
                "current_evidence_findings": [
                    {
                        "source": "police-report.pdf",
                        "finding": "Rear-end collision; vehicle reportedly drivable.",
                    },
                    {
                        "source": "initial-damage.jpg",
                        "finding": "Front damage and tow-truck condition are visible.",
                    },
                    {
                        "source": "followup-identity.jpg",
                        "finding": "Plate verified and rear damage is visible.",
                    },
                ],
                "conflicts": [
                    {
                        "field": "damage_location",
                        "values": ["rear", "front"],
                        "sources": [
                            "police-report.pdf",
                            "initial-damage.jpg",
                            "followup-identity.jpg",
                        ],
                        "reason": "The initial photo remains inconsistent.",
                    }
                ],
            }
        )

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        created = self.repository.create_human_review.call_args.args[0]
        known = "\n".join(created.briefing.known_facts)
        self.assertIn("police-report.pdf", known)
        self.assertIn("initial-damage.jpg", known)
        self.assertIn("followup-identity.jpg", known)
        self.assertEqual(
            created.briefing.recommended_next_action,
            "Verify whether initial-damage.jpg belongs to this claim.",
        )

    def test_voice_date_with_injury_removes_resolved_missing_date_context(self) -> None:
        stale = (
            "The exact date and time of the incident are not specified in the "
            "provided evidence."
        )
        optional_time = "The incident time remains unknown."
        self.repository.get_claim.return_value.update({
            "conflicts": [],
            "incident_date": "2026-08-24",
            "incident_date_provenance": {
                "source_type": "claimant_voice",
                "document_id": "DOC-VOICE",
            },
            "human_review_reason": "Possible injury requires adjuster review.",
            "operational_indicators": {"possible_injury": True},
            "unresolved_uncertainties": [
                {"uncertainty": stale},
                {"uncertainty": optional_time},
            ],
        })

        review = self.service.ensure_review_requested(
            CLAIM_ID, correlation_id="corr-voice-injury"
        )

        self.assertEqual(
            review.briefing.summary,
            "FirstNotice paused automated routing because possible injury requires "
            "adjuster review.",
        )
        self.assertNotIn(stale, review.briefing.unresolved_questions)
        self.assertNotIn(optional_time, review.briefing.unresolved_questions)
        email = self.gmail.send_human_review_email.call_args.args[0]
        self.assertNotIn(stale, email.unresolved_questions)
        self.assertNotIn(optional_time, email.unresolved_questions)

    def test_voice_date_and_time_with_injury_remove_resolved_date_context(self) -> None:
        stale = (
            "The exact date and time of the incident are not specified in the "
            "provided evidence."
        )
        self.repository.get_claim.return_value.update({
            "conflicts": [],
            "incident_date": "2026-08-24",
            "incident_time": "18:00",
            "incident_date_provenance": {
                "source_type": "claimant_voice",
                "document_id": "DOC-VOICE",
            },
            "human_review_reason": "Possible injury requires adjuster review.",
            "operational_indicators": {"possible_injury": True},
            "unresolved_uncertainties": [{"uncertainty": stale}],
        })

        review = self.service.ensure_review_requested(
            CLAIM_ID, correlation_id="corr-voice-date-time-injury"
        )

        self.assertNotIn(stale, review.briefing.unresolved_questions)
        self.assertEqual(review.status, "pending")

    def test_genuinely_missing_incident_date_context_remains(self) -> None:
        unresolved = (
            "The exact date and time of the incident are not specified in the "
            "provided evidence."
        )
        self.repository.get_claim.return_value.update({
            "conflicts": [],
            "incident_date": None,
            "unresolved_uncertainties": [{"uncertainty": unresolved}],
        })

        review = self.service.ensure_review_requested(
            CLAIM_ID, correlation_id="corr-date-unresolved"
        )

        self.assertIn(
            f"Resolve whether this remains consequential: {unresolved}",
            review.briefing.unresolved_questions,
        )

    def test_unrelated_drivability_context_remains_after_voice_date_resolution(self) -> None:
        stale_date = "The incident date was not provided."
        drivability = "Vehicle drivability remains unknown."
        self.repository.get_claim.return_value.update({
            "conflicts": [],
            "incident_date": "2026-08-24",
            "incident_date_provenance": {
                "source_type": "claimant_voice",
                "document_id": "DOC-VOICE",
            },
            "unresolved_uncertainties": [
                {"uncertainty": stale_date},
                {"uncertainty": drivability},
            ],
        })

        review = self.service.ensure_review_requested(
            CLAIM_ID, correlation_id="corr-drivability"
        )

        context = "\n".join(review.briefing.unresolved_questions)
        self.assertNotIn(stale_date, context)
        self.assertIn(drivability, context)

    def test_checkpoint_carries_exact_conflicting_source_document_id(self) -> None:
        self.repository.get_claim.return_value["conflicts"] = [
            {
                "field": "damage_location",
                "values": ["front", "rear"],
                "sources": ["initial-damage.jpg", "police-report.pdf"],
                "reason": "Damage locations differ.",
            }
        ]
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-DAMAGE",
                claim_id=CLAIM_ID,
                document_type="damage_evidence",
                filename="initial-damage.jpg",
                received_at=NOW,
            ),
            ClaimDocument(
                document_id="DOC-REPORT",
                claim_id=CLAIM_ID,
                document_type="police_report",
                filename="police-report.pdf",
                received_at=NOW,
            ),
        ]

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        created = self.repository.create_human_review.call_args.args[0]
        damage = next(
            item
            for item in created.source_references
            if item.document_id == "DOC-DAMAGE"
        )
        self.assertEqual(damage.filename, "initial-damage.jpg")
        self.assertTrue(damage.replacement_eligible)

    def test_duplicate_filenames_are_not_exposed_as_replacement_targets(self) -> None:
        self.repository.get_claim.return_value["conflicts"] = [
            {
                "field": "damage_location",
                "sources": ["damage.jpg", "police-report.pdf"],
            }
        ]
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id=f"DOC-{index}",
                claim_id=CLAIM_ID,
                document_type="damage_evidence",
                filename="damage.jpg",
                received_at=NOW,
            )
            for index in (1, 2)
        ]

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        created = self.repository.create_human_review.call_args.args[0]
        self.assertEqual(created.source_references, [])

    def test_checkpoint_source_references_include_uncertainty_sources(self) -> None:
        self.repository.get_claim.return_value.update(
            {
                "conflicts": [],
                "unresolved_uncertainties": [{
                    "uncertainty": "Two photos appear to show different vehicles.",
                    "sources": ["first.jpg", "second.jpg"],
                }],
            }
        )
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-FIRST",
                claim_id=CLAIM_ID,
                document_type="damage_evidence",
                filename="first.jpg",
                received_at=NOW,
            ),
            ClaimDocument(
                document_id="DOC-SECOND",
                claim_id=CLAIM_ID,
                document_type="damage_evidence",
                filename="second.jpg",
                received_at=NOW,
            ),
        ]

        self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        created = self.repository.create_human_review.call_args.args[0]
        self.assertEqual(
            {reference.document_id for reference in created.source_references},
            {"DOC-FIRST", "DOC-SECOND"},
        )
        self.assertEqual(len(created.unresolved_uncertainties), 1)

    def test_free_text_never_authorizes_checkpoint_replacement_target(self) -> None:
        source = EvidenceSourceReference(
            document_id="DOC-DAMAGE",
            filename="initial-damage.jpg",
            document_type="damage_evidence",
            conflict_fields=["damage_location"],
            replacement_eligible=True,
        )
        pending = record(
            source_references=[source],
            recommended_remediation=RecommendedRemediation(
                type="upload_document",
                label="Request a replacement damage photo.",
                instruction="Please upload the correct damage photo for this claim.",
                document_type="damage_evidence",
            ),
            recommended_target_document_id="DOC-DAMAGE",
        )
        self.repository.get_human_review_by_token_hash.return_value = pending
        self.repository.get_document.return_value = ClaimDocument(
            document_id="DOC-DAMAGE",
            claim_id=CLAIM_ID,
            document_type="damage_evidence",
            filename="initial-damage.jpg",
            received_at=NOW,
        )
        decided = pending.model_copy(
            update={
                "status": "correction_requested",
                "correction_type": "upload_document",
                "target_document_id": None,
            }
        )
        self.repository.decide_human_review.return_value = (decided, False)

        self.service.request_correction(
            "secure-token",
            HumanReviewDecisionRequest(
                decision_note="Please upload a clearer vehicle photo.",
                correction_type="text",
                target_document_id="DOC-BROWSER-OVERRIDE",
            ),
        )

        call = self.repository.decide_human_review.call_args.kwargs
        self.assertEqual(call["correction_type"], "upload_document")
        self.assertIsNone(call["target_document_id"])

    def test_adjuster_can_request_multiple_structured_evidence_items(self) -> None:
        pending = record()
        self.repository.get_human_review_by_token_hash.return_value = pending
        decided = pending.model_copy(
            update={"status": "correction_requested", "correction_type": "upload_document"}
        )
        self.repository.decide_human_review.return_value = (decided, False)
        requested = [
            HumanReviewEvidenceRequest(
                document_type="policy_document",
                instruction="Please upload the correct policy document.",
            ),
            HumanReviewEvidenceRequest(
                document_type="police_report",
                instruction="Please upload the correct police report.",
            ),
        ]

        self.service.request_correction(
            "secure-token",
            HumanReviewDecisionRequest(requested_evidence=requested),
        )

        call = self.repository.decide_human_review.call_args.kwargs
        self.assertEqual(call["correction_type"], "upload_document")
        self.assertEqual(call["requested_evidence"], [
            item.model_dump(mode="python") for item in requested
        ])
        self.assertIsNone(call["target_document_id"])

    def test_structured_replacement_target_is_validated_and_persisted(self) -> None:
        pending = record()
        self.repository.get_human_review_by_token_hash.return_value = pending
        self.repository.get_document.return_value = ClaimDocument(
            document_id="DOC-POLICY",
            claim_id=CLAIM_ID,
            document_type="policy_document",
            filename="old-policy.pdf",
            status="validated",
            received_at=NOW,
        )
        self.repository.decide_human_review.return_value = (
            pending.model_copy(update={"status": "correction_requested"}),
            False,
        )
        requested = HumanReviewEvidenceRequest(
            document_type="policy_document",
            instruction="Upload the correct policy document.",
            replaces_document_id="DOC-POLICY",
        )

        self.service.request_correction(
            "secure-token",
            HumanReviewDecisionRequest(requested_evidence=[requested]),
        )

        self.repository.get_document.assert_called_once_with(CLAIM_ID, "DOC-POLICY")
        persisted = self.repository.decide_human_review.call_args.kwargs[
            "requested_evidence"
        ]
        self.assertEqual(persisted[0]["replaces_document_id"], "DOC-POLICY")

    def test_invalid_structured_replacement_targets_are_rejected(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = record()
        invalid_targets = [
            None,
            ClaimDocument(
                document_id="DOC-TARGET", claim_id="CLM-OTHER",
                document_type="policy_document", filename="other.pdf",
                status="validated", received_at=NOW,
            ),
            ClaimDocument(
                document_id="DOC-TARGET", claim_id=CLAIM_ID,
                document_type="policy_document", filename="old.pdf",
                status="superseded", received_at=NOW,
            ),
            ClaimDocument(
                document_id="DOC-TARGET", claim_id=CLAIM_ID,
                document_type="damage_evidence", filename="photo.jpg",
                status="validated", received_at=NOW,
            ),
        ]
        for target in invalid_targets:
            with self.subTest(target=target):
                self.repository.get_document.return_value = target
                with self.assertRaisesRegex(
                    HumanReviewConflictError, "active matching document"
                ):
                    self.service.request_correction(
                        "secure-token",
                        HumanReviewDecisionRequest(requested_evidence=[
                            HumanReviewEvidenceRequest(
                                document_type="policy_document",
                                instruction="Upload the correct policy document.",
                                replaces_document_id="DOC-TARGET",
                            )
                        ]),
                    )
        self.repository.decide_human_review.assert_not_called()

    def test_unrelated_or_superseded_replacement_target_is_rejected(self) -> None:
        source = EvidenceSourceReference(
            document_id="DOC-DAMAGE",
            filename="initial-damage.jpg",
            document_type="damage_evidence",
            replacement_eligible=True,
        )
        self.repository.get_human_review_by_token_hash.return_value = record(
            source_references=[source],
            recommended_remediation=RecommendedRemediation(
                type="upload_document",
                label="Request a replacement damage photo.",
                instruction="Please upload the correct damage photo for this claim.",
                document_type="damage_evidence",
            ),
            recommended_target_document_id="DOC-DAMAGE",
        )
        self.repository.get_document.return_value = ClaimDocument(
            document_id="DOC-DAMAGE",
            claim_id="CLM-OTHER",
            document_type="damage_evidence",
            filename="initial-damage.jpg",
            status="superseded",
            received_at=NOW,
        )

        with self.assertRaises(HumanReviewConflictError):
            self.service.request_correction(
                "secure-token",
                HumanReviewDecisionRequest(
                    correction_type="replace_document",
                    target_document_id="DOC-DAMAGE",
                ),
            )

        self.repository.decide_human_review.assert_not_called()

    def test_ambiguous_recommendation_cannot_create_unsafe_action(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = record(
            recommended_remediation=RecommendedRemediation(
                type="upload_document",
                label="Manual evidence selection is required.",
                instruction="Multiple artifacts may require replacement.",
                can_request=False,
            )
        )

        with self.assertRaisesRegex(HumanReviewConflictError, "Describe"):
            self.service.request_correction(
                "secure-token", HumanReviewDecisionRequest()
            )

        self.repository.decide_human_review.assert_not_called()

    def test_duplicate_checkpoint_reuses_sent_review_without_email(self) -> None:
        current = record(notification_status="sent", status="correction_requested")
        self.repository.get_claim.return_value.update(
            {
                "current_human_review_generation": 1,
                "current_human_review_generation_key": f"{CLAIM_ID}:submitted-review:v1",
                "current_human_review_id": REVIEW_ID,
            }
        )
        self.repository.get_human_review.return_value = current

        first = self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")
        second = self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        self.assertEqual(first.review_id, second.review_id)
        self.repository.create_human_review.assert_not_called()
        self.repository.reserve_human_review_generation.assert_not_called()
        self.gmail.send_human_review_email.assert_not_called()

    def test_review_email_failure_does_not_continue_claim(self) -> None:
        self.gmail.send_human_review_email.side_effect = GmailError(
            "temporary", retryable=True
        )

        with self.assertRaises(GmailError):
            self.service.ensure_review_requested(CLAIM_ID, correlation_id="corr-review")

        self.publisher.publish.assert_not_called()
        created_review = self.repository.create_human_review.call_args.args[0]
        self.repository.mark_human_review_notification.assert_called_with(
            CLAIM_ID,
            created_review.review_id,
            status="failed",
            correlation_id="corr-review",
            review_generation=1,
        )

    def test_email_retry_rotates_token_within_same_generation(self) -> None:
        cycle_two_id = human_review_id(CLAIM_ID, 2)
        failed = record(
            review_id=cycle_two_id,
            generation=2,
            generation_key=f"{CLAIM_ID}:DOC-2:resume",
            notification_status="failed",
        )
        self.repository.get_claim.return_value.update(
            {
                "current_human_review_generation": 2,
                "current_human_review_generation_key": failed.generation_key,
                "current_human_review_id": cycle_two_id,
            }
        )
        self.repository.get_human_review.return_value = failed

        review = self.service.ensure_review_requested(
            CLAIM_ID, correlation_id="corr-cycle-2"
        )

        self.assertEqual(review.review_id, cycle_two_id)
        self.repository.create_human_review.assert_not_called()
        self.repository.replace_human_review_token.assert_called_once()
        self.gmail.send_human_review_email.assert_called_once()

    def test_invalid_and_expired_tokens_are_rejected(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = None
        with self.assertRaisesRegex(Exception, "invalid"):
            self.service.get_public_review("invalid-token")

        self.repository.get_human_review_by_token_hash.return_value = record(
            expires_at=NOW - timedelta(minutes=1)
        )
        with patch("app.services.human_review_service.datetime") as clock:
            clock.now.return_value = NOW
            clock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            with self.assertRaises(HumanReviewExpiredError):
                self.service.get_public_review("expired-token")

    def test_public_review_exposes_active_filenames_without_internal_ids(self) -> None:
        sources = [
            EvidenceSourceReference(
                document_id="DOC-ACTIVE", filename="active-policy.pdf",
                document_type="policy_document", replacement_eligible=True,
            ),
            EvidenceSourceReference(
                document_id="DOC-SUPERSEDED", filename="old-policy.pdf",
                document_type="policy_document", replacement_eligible=True,
            ),
        ]
        self.repository.get_human_review_by_token_hash.return_value = record(
            source_references=sources
        )
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-ACTIVE", claim_id=CLAIM_ID,
                document_type="policy_document", filename="active-policy.pdf",
                status="validated", received_at=NOW,
            ),
            ClaimDocument(
                document_id="DOC-SUPERSEDED", claim_id=CLAIM_ID,
                document_type="policy_document", filename="old-policy.pdf",
                status="superseded", received_at=NOW,
            ),
        ]

        public = self.service.get_public_review("secure-token")

        self.assertEqual(len(public.source_references), 1)
        self.assertEqual(public.source_references[0].filename, "active-policy.pdf")
        self.assertNotIn(
            "DOC-ACTIVE", public.model_dump_json()
        )

    def test_public_review_exposes_received_medical_attachment(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = record()
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-MEDICAL",
                claim_id=CLAIM_ID,
                document_type="medical_document",
                filename="medical-record.pdf",
                status="validated",
                object_name="claims/claim/documents/medical/medical-record.pdf",
                received_at=NOW,
            )
        ]

        public = self.service.get_public_review("secure-token")

        self.assertEqual(len(public.supporting_documents), 1)
        self.assertEqual(
            public.supporting_documents[0].filename, "medical-record.pdf"
        )
        self.assertEqual(
            public.supporting_documents[0].document_type, "medical_document"
        )

    def test_public_review_exposes_only_grounded_claimant_voice_facts(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = record()
        self.repository.get_claim.return_value.update({
            "voice_injury_source_document_id": "DOC-VOICE",
        })
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-VOICE",
                claim_id=CLAIM_ID,
                document_type="voice_note",
                source_type="claimant_voice",
                filename="incident.webm",
                status="validated",
                evidence_facts={
                    "incident_date": "2026-08-24",
                    "incident_time": "18:00",
                    "injury_mentioned": "true",
                    "injury_description": "neck pain later that evening",
                    "vehicle_identity": "must not be exposed here",
                },
                received_at=NOW,
            )
        ]

        public = self.service.get_public_review("secure-token")

        self.assertEqual(len(public.claimant_voice_updates), 1)
        update = public.claimant_voice_updates[0]
        self.assertEqual(update.source_label, "Claimant voice response")
        self.assertEqual(update.incident_date, "2026-08-24")
        self.assertEqual(update.incident_time, "18:00")
        self.assertTrue(update.injury_mentioned)
        self.assertEqual(update.injury_description, "neck pain later that evening")
        self.assertTrue(update.contributed_to_decision)
        self.assertNotIn("vehicle_identity", update.model_dump_json())

    def test_public_review_projects_actual_document_type_for_evidence(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = record()
        self.repository.get_claim.return_value["current_evidence_findings"] = [{
            "source": "insurance.pdf",
            "finding": "Policy evidence was validated.",
        }]
        self.repository.get_documents.return_value = [
            ClaimDocument(
                document_id="DOC-POLICY",
                claim_id=CLAIM_ID,
                document_type="policy_document",
                filename="insurance.pdf",
                status="validated",
                received_at=NOW,
            )
        ]

        public = self.service.get_public_review("secure-token")

        self.assertEqual(
            public.evidence_comparison[0]["document_type"], "policy_document"
        )

    def test_medical_attachment_download_is_scoped_to_review_claim(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = record()
        self.repository.get_document.return_value = ClaimDocument(
            document_id="DOC-MEDICAL",
            claim_id=CLAIM_ID,
            document_type="medical_document",
            filename="medical-record.pdf",
            status="validated",
            object_name="claims/claim/documents/medical/medical-record.pdf",
            content_type="application/pdf",
            received_at=NOW,
        )
        storage = MagicMock()
        storage.download_claim_document.return_value = b"medical-bytes"
        self.service._storage_service = storage

        content, filename, content_type = self.service.get_supporting_document(
            "secure-token", "DOC-MEDICAL"
        )

        self.assertEqual(content, b"medical-bytes")
        self.assertEqual(filename, "medical-record.pdf")
        self.assertEqual(content_type, "application/pdf")
        storage.download_claim_document.assert_called_once_with(
            "claims/claim/documents/medical/medical-record.pdf"
        )

    def test_approve_consumes_token_and_publishes_versioned_event(self) -> None:
        approved = record(
            status="approved",
            decision_at=NOW,
            decision_event_id=f"{CLAIM_ID}:{REVIEW_ID}:approved:v1",
            decision_publish_status="pending",
        )
        self.repository.decide_human_review.return_value = (approved, False)

        result = self.service.approve("secure-token", HumanReviewDecisionRequest())

        event = self.publisher.publish.call_args.args[0]
        self.assertIsInstance(event, ClaimHumanReviewApprovedEvent)
        self.assertEqual(event.payload.review_id, REVIEW_ID)
        self.assertNotIn("secure-token", event.model_dump_json())
        self.assertEqual(result.status, "approved")

    def test_duplicate_approve_is_idempotent_and_does_not_republish(self) -> None:
        approved = record(
            status="approved",
            decision_at=NOW,
            decision_event_id=f"{CLAIM_ID}:{REVIEW_ID}:approved:v1",
            decision_publish_status="published",
        )
        self.repository.decide_human_review.return_value = (approved, True)

        result = self.service.approve("secure-token", HumanReviewDecisionRequest())

        self.assertTrue(result.duplicate)
        self.publisher.publish.assert_not_called()

    def test_cycle_two_decision_targets_only_cycle_two_review(self) -> None:
        cycle_two_id = human_review_id(CLAIM_ID, 2)
        approved = record(
            review_id=cycle_two_id,
            generation=2,
            generation_key=f"{CLAIM_ID}:DOC-2:resume",
            status="approved",
            decision_at=NOW,
            decision_event_id=f"{CLAIM_ID}:{cycle_two_id}:approved:v1",
            decision_publish_status="pending",
        )
        self.repository.decide_human_review.return_value = (approved, False)

        self.service.approve("cycle-two-token", HumanReviewDecisionRequest())

        event = self.publisher.publish.call_args.args[0]
        self.assertEqual(event.payload.review_id, cycle_two_id)
        timeline = self.repository.append_claim_event.call_args.kwargs
        self.assertEqual(timeline["details"]["review_generation"], 2)
        self.assertNotEqual(event.payload.review_id, human_review_id(CLAIM_ID, 1))

    def test_correction_decision_publishes_distinct_event(self) -> None:
        correction = record(
            status="correction_requested",
            decision_at=NOW,
            decision_event_id=f"{CLAIM_ID}:{REVIEW_ID}:correction_requested:v1",
            decision_publish_status="pending",
        )
        self.repository.decide_human_review.return_value = (correction, False)
        self.repository.get_human_review_by_token_hash.return_value = record()

        self.service.request_correction(
            "secure-token",
            HumanReviewDecisionRequest(
                decision_note="Please provide the correct policy number."
            ),
        )

        self.assertIsInstance(
            self.publisher.publish.call_args.args[0],
            ClaimHumanReviewCorrectionRequestedEvent,
        )

    def test_inspection_decision_more_info_maps_prose_to_allowlisted_upload(self) -> None:
        pending = record()
        self.repository.get_human_review_by_token_hash.return_value = pending
        self.repository.get_claim.return_value.update({
            "status": "inspection_ready",
            "current_human_review_id": REVIEW_ID,
        })
        decided = pending.model_copy(update={
            "status": "correction_requested",
            "correction_type": "upload_document",
            "decision_note": "Please upload a clearer rear damage photo.",
            "decision_at": NOW,
            "decision_event_id": f"{CLAIM_ID}:{REVIEW_ID}:correction_requested:v1",
            "decision_publish_status": "pending",
        })
        self.repository.decide_human_review.return_value = (decided, False)

        self.service.request_correction(
            "secure-token",
            HumanReviewDecisionRequest(
                decision_note="Please upload a clearer rear damage photo."
            ),
        )

        decision = self.repository.decide_human_review.call_args.kwargs
        self.assertEqual(decision["correction_type"], "upload_document")
        self.assertIsNone(decision["target_document_id"])

    def test_free_text_requests_map_to_one_allowlisted_action(self) -> None:
        cases = [
            ("Please upload the correct policy document.", "upload_document"),
            ("Please upload the police report.", "upload_document"),
            ("Please upload a clearer vehicle photo.", "upload_document"),
            ("Please provide the correct policy number.", "text"),
        ]
        for instruction, expected_type in cases:
            with self.subTest(instruction=instruction):
                self.repository.reset_mock()
                pending = record()
                decided = pending.model_copy(update={
                    "status": "correction_requested",
                    "correction_type": expected_type,
                })
                self.repository.get_human_review_by_token_hash.return_value = pending
                self.repository.get_claim.return_value = {
                    "claim_id": CLAIM_ID,
                    "status": "human_review_required",
                    "current_human_review_id": REVIEW_ID,
                }
                self.repository.decide_human_review.return_value = (decided, False)

                self.service.request_correction(
                    "secure-token",
                    HumanReviewDecisionRequest(decision_note=instruction),
                )

                call = self.repository.decide_human_review.call_args.kwargs
                self.assertEqual(call["correction_type"], expected_type)
                self.assertEqual(call["requested_evidence"], [])
                self.assertIsNone(call["target_document_id"])

    def test_vague_free_text_keeps_review_pending(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = record()
        with self.assertRaisesRegex(HumanReviewConflictError, "specific supported"):
            self.service.request_correction(
                "secure-token",
                HumanReviewDecisionRequest(
                    decision_note="I need something else about the vehicle."
                ),
            )
        self.repository.decide_human_review.assert_not_called()

    def test_medical_document_request_preserves_exact_instruction(self) -> None:
        instruction = (
            "Please upload medical documentation related to the reported injury."
        )
        pending = record()
        decided = pending.model_copy(update={
            "status": "correction_requested",
            "correction_type": "upload_document",
            "decision_note": instruction,
        })
        self.repository.get_human_review_by_token_hash.return_value = pending
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "current_human_review_id": REVIEW_ID,
        }
        self.repository.decide_human_review.return_value = (decided, False)

        self.service.request_correction(
            "secure-token", HumanReviewDecisionRequest(decision_note=instruction)
        )

        call = self.repository.decide_human_review.call_args.kwargs
        self.assertEqual(call["correction_type"], "upload_document")
        self.assertEqual(call["decision_note"], instruction)

    def test_vague_injury_request_is_rejected(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = record()
        with self.assertRaisesRegex(HumanReviewConflictError, "specific supported"):
            self.service.request_correction(
                "secure-token",
                HumanReviewDecisionRequest(
                    decision_note="Please explain more about the injury."
                ),
            )
        self.repository.decide_human_review.assert_not_called()

    def test_manual_handling_is_durable_and_publishes_distinct_event(self) -> None:
        pending = record()
        decided = pending.model_copy(update={
            "status": "manual_handling",
            "decision_event_id": f"{CLAIM_ID}:{REVIEW_ID}:manual_handling:v1",
            "decision_publish_status": "pending",
        })
        self.repository.get_human_review_by_token_hash.return_value = pending
        self.repository.decide_human_review.return_value = (decided, False)

        result = self.service.continue_manual_handling(
            "secure-token", HumanReviewDecisionRequest()
        )

        self.assertEqual(result.status, "manual_handling")
        self.assertIsInstance(
            self.publisher.publish.call_args.args[0],
            ClaimHumanReviewManualHandlingEvent,
        )

    def test_duplicate_manual_handling_is_idempotent(self) -> None:
        decided = record(
            status="manual_handling",
            decision_event_id=f"{CLAIM_ID}:{REVIEW_ID}:manual_handling:v1",
            decision_publish_status="published",
        )
        self.repository.get_human_review_by_token_hash.return_value = decided
        self.repository.decide_human_review.return_value = (decided, True)

        result = self.service.continue_manual_handling(
            "secure-token", HumanReviewDecisionRequest()
        )

        self.assertTrue(result.duplicate)
        self.publisher.publish.assert_not_called()

    def test_stale_inspection_decision_generation_cannot_mutate_claim(self) -> None:
        self.repository.get_human_review_by_token_hash.return_value = record()
        self.repository.get_claim.return_value.update({
            "status": "inspection_ready",
            "current_human_review_id": "HRV-NEWER",
        })

        with self.assertRaisesRegex(HumanReviewConflictError, "no longer current"):
            self.service.approve("secure-token", HumanReviewDecisionRequest())

        self.repository.decide_human_review.assert_not_called()


class HumanReviewResumeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.workflow = HumanReviewResumeWorkflow(self.repository)

    def test_approval_resumes_same_claim_to_inspection_when_complete(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "conflicts": [{"field": "policy_number"}],
            "missing_documents": [],
            "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(status="approved")

        result = self.workflow.resume_approved(CLAIM_ID, REVIEW_ID, "corr")

        self.assertEqual(result["final_status"], "inspection_pending")

    def test_inspection_ready_approval_starts_existing_dispatch_boundary(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "inspection_ready",
            "current_human_review_id": REVIEW_ID,
            "conflicts": [], "missing_documents": [], "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(status="approved")

        result = self.workflow.resume_approved(CLAIM_ID, REVIEW_ID, "corr")

        self.assertEqual(result["final_status"], "inspection_pending")
        self.assertEqual(
            self.repository.complete_human_review_resume.call_args.kwargs[
                "target_status"
            ],
            ClaimStatus.INSPECTION_PENDING,
        )

    def test_voice_correction_passes_injury_signal_to_deterministic_completion(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "review_processing",
            "pending_corrections": {"incident_date": "2026-08-24"},
        }
        self.repository.complete_claim_correction.return_value = (
            ClaimStatus.HUMAN_REVIEW_REQUIRED
        )

        result = self.workflow.resume_correction(
            CLAIM_ID,
            "AUTONOMOUS-DATE",
            "incident_date",
            "voice-correlation",
            source_type="claimant_voice",
            source_document_id="DOC-VOICE",
            injury_mentioned=True,
            injury_description="neck pain later that evening",
        )

        self.assertEqual(result["final_status"], "human_review_required")
        call = self.repository.complete_claim_correction.call_args.kwargs
        self.assertEqual(call["source_type"], "claimant_voice")
        self.assertEqual(call["source_document_id"], "DOC-VOICE")
        self.assertTrue(call["injury_mentioned"])

    def test_adjuster_prose_creates_non_replacement_upload_action(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "inspection_ready",
            "current_human_review_id": REVIEW_ID,
            "conflicts": [], "missing_documents": [], "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(
            status="correction_requested",
            correction_type="upload_document",
            decision_note="Please upload a clearer passenger-side rear damage photo.",
        )

        result = self.workflow.request_correction(CLAIM_ID, REVIEW_ID, "corr")

        action = self.repository.complete_human_review_resume.call_args.kwargs[
            "requested_actions"
        ][0]
        self.assertEqual(result["final_status"], "awaiting_documents")
        self.assertEqual(action["document_type"], "damage_evidence")
        self.assertIsNone(action["replaces_document_id"])
        call = self.repository.complete_human_review_resume.call_args.kwargs
        self.assertEqual(call["claim_id"], CLAIM_ID)
        self.assertEqual(call["conflicts"], [])

    def test_adjuster_prose_builds_one_typed_claimant_action(self) -> None:
        cases = [
            ("Please upload the correct policy document.", "upload_document", "policy_document"),
            ("Please upload the police report.", "upload_document", "police_report"),
            (
                "Please upload medical documentation related to the reported injury.",
                "upload_document",
                "medical_document",
            ),
            ("Please upload a clearer vehicle photo.", "upload_document", "damage_evidence"),
            ("Please provide the correct policy number.", "enter_text", "policy_number"),
        ]
        for instruction, action_type, value in cases:
            with self.subTest(instruction=instruction):
                self.repository.reset_mock()
                self.repository.get_claim.return_value = {
                    "claim_id": CLAIM_ID,
                    "status": "human_review_required",
                    "current_human_review_id": REVIEW_ID,
                    "conflicts": [],
                    "missing_documents": [],
                    "unusable_evidence": [],
                }
                self.repository.get_human_review.return_value = record(
                    status="correction_requested",
                    correction_type=(
                        "upload_document" if action_type == "upload_document" else "text"
                    ),
                    decision_note=instruction,
                )

                self.workflow.request_correction(CLAIM_ID, REVIEW_ID, "corr")

                actions = self.repository.complete_human_review_resume.call_args.kwargs[
                    "requested_actions"
                ]
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]["action_type"], action_type)
                self.assertEqual(
                    actions[0][
                        "document_type" if action_type == "upload_document" else "field_name"
                    ],
                    value,
                )
                self.assertIsNone(actions[0].get("replaces_document_id"))

    def test_manual_handling_keeps_claim_manual_without_remediation(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "current_human_review_id": REVIEW_ID,
        }
        self.repository.get_human_review.return_value = record(
            status="manual_handling"
        )

        result = self.workflow.continue_manual_handling(
            CLAIM_ID, REVIEW_ID, "corr"
        )

        self.assertEqual(result["final_status"], "human_review_required")
        self.repository.complete_manual_human_review.assert_called_once_with(
            claim_id=CLAIM_ID,
            review_id=REVIEW_ID,
            correlation_id="corr",
        )
        self.repository.complete_human_review_resume.assert_not_called()

    def test_multiple_adjuster_evidence_items_become_distinct_actions(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "current_human_review_id": REVIEW_ID,
            "conflicts": [], "missing_documents": [], "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(
            status="correction_requested",
            correction_type="upload_document",
            requested_evidence=[
                HumanReviewEvidenceRequest(
                    document_type="policy_document",
                    instruction="Please upload the correct policy document.",
                    replaces_document_id="DOC-POLICY",
                ),
                HumanReviewEvidenceRequest(
                    document_type="police_report",
                    instruction="Please upload the correct police report.",
                ),
            ],
        )

        result = self.workflow.request_correction(CLAIM_ID, REVIEW_ID, "corr")

        call = self.repository.complete_human_review_resume.call_args.kwargs
        self.assertEqual(result["final_status"], "awaiting_documents")
        self.assertEqual(
            [item["document_type"] for item in call["requested_actions"]],
            ["policy_document", "police_report"],
        )
        self.assertEqual(
            [item["replaces_document_id"] for item in call["requested_actions"]],
            ["DOC-POLICY", None],
        )
        self.assertEqual(len({item["action_id"] for item in call["requested_actions"]}), 2)
        self.assertEqual(
            [item["type"] for item in call["missing_documents"]],
            ["policy_document", "police_report"],
        )

    def test_approval_does_not_bypass_unresolved_missing_requirements(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "conflicts": [{"field": "policy_number"}],
            "missing_documents": [{"type": "police_report"}],
            "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(status="approved")

        result = self.workflow.resume_approved(CLAIM_ID, REVIEW_ID, "corr")

        self.assertEqual(result["final_status"], "awaiting_documents")

    def test_approval_records_exact_fingerprint_and_keeps_different_same_field_conflict(self) -> None:
        approved_conflict = EvidenceConflict(
            field="damage_location", values=["rear", "front"],
            sources=["report.pdf", "front.jpg"], reason="First disagreement.",
        )
        different_conflict = {
            "field": "damage_location", "values": ["left", "right"],
            "sources": ["a.jpg", "b.jpg"], "reason": "Different disagreement.",
        }
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID, "status": "human_review_required",
            "conflicts": [approved_conflict.model_dump(mode="python"), different_conflict],
            "source_aware_conflicts": [
                {"fingerprint": "CFP-APPROVED"},
                {"fingerprint": "CFP-DIFFERENT"},
            ],
            "unresolved_uncertainties": [],
            "approved_issue_fingerprints": ["CFP-OLDER"],
            "missing_documents": [], "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(
            status="approved", conflicts=[approved_conflict],
            issue_fingerprints=["CFP-APPROVED"],
        )

        self.workflow.resume_approved(CLAIM_ID, REVIEW_ID, "corr")

        call = self.repository.complete_human_review_resume.call_args.kwargs
        self.assertEqual(call["conflicts"], [different_conflict])
        self.assertEqual(
            call["approved_issue_fingerprints"],
            ["CFP-OLDER", "CFP-APPROVED"],
        )
        self.assertEqual(
            call["source_aware_conflicts"], [{"fingerprint": "CFP-DIFFERENT"}]
        )

    def test_replacement_action_uses_damage_capability_but_targets_plate_artifact(self) -> None:
        source = EvidenceSourceReference(
            document_id="DOC-FRONT", filename="front-plate.jpg",
            document_type="license_plate_photo", replacement_eligible=True,
        )
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID, "status": "human_review_required",
            "conflicts": [], "missing_documents": [], "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(
            status="correction_requested", correction_type="replace_document",
            target_document_id="DOC-FRONT", source_references=[source],
            recommended_remediation=RecommendedRemediation(
                type="upload_document", label="Request a replacement damage photo.",
                instruction="Please upload the correct damage photo for this claim.",
                document_type="damage_evidence",
            ),
        )
        self.repository.get_document.return_value = ClaimDocument(
            document_id="DOC-FRONT", claim_id=CLAIM_ID,
            document_type="license_plate_photo", filename="front-plate.jpg",
            received_at=NOW,
        )

        self.workflow.request_correction(CLAIM_ID, REVIEW_ID, "corr")

        action = self.repository.complete_human_review_resume.call_args.kwargs[
            "requested_actions"
        ][0]
        self.assertEqual(action["document_type"], "damage_evidence")
        self.assertEqual(action["replaces_document_id"], "DOC-FRONT")

    def test_correction_request_reuses_claimant_requested_action(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "conflicts": [{"field": "policy_number"}],
            "missing_documents": [],
            "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(
            status="correction_requested"
        )

        result = self.workflow.request_correction(CLAIM_ID, REVIEW_ID, "corr")

        self.assertEqual(result["final_status"], "awaiting_documents")
        action = self.repository.complete_human_review_resume.call_args.kwargs[
            "requested_actions"
        ][0]
        self.assertEqual(action["action_type"], "enter_text")
        self.assertTrue(action["action_id"].startswith("ACT-"))
        self.assertEqual(action["field_name"], "policy_number")

    def test_incident_date_correction_still_creates_enter_text(self) -> None:
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "conflicts": [{"field": "incident_date"}],
            "missing_documents": [],
            "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(
            status="correction_requested",
            conflict_fields=["incident_date"],
            correction_type="text",
        )

        self.workflow.request_correction(CLAIM_ID, REVIEW_ID, "corr")

        action = self.repository.complete_human_review_resume.call_args.kwargs[
            "requested_actions"
        ][0]
        self.assertEqual(action["action_type"], "enter_text")
        self.assertEqual(action["field_name"], "incident_date")

    def test_evidence_correction_creates_upload_document_action(self) -> None:
        source = EvidenceSourceReference(
            document_id="DOC-DAMAGE",
            filename="initial-damage.jpg",
            document_type="damage_evidence",
            conflict_fields=["damage_location"],
            replacement_eligible=True,
        )
        self.repository.get_claim.return_value = {
            "claim_id": CLAIM_ID,
            "status": "human_review_required",
            "conflicts": [{"field": "damage_location"}],
            "missing_documents": [],
            "unusable_evidence": [],
        }
        self.repository.get_human_review.return_value = record(
            status="correction_requested",
            correction_type="replace_document",
            target_document_id="DOC-DAMAGE",
            source_references=[source],
        )
        self.repository.get_document.return_value = ClaimDocument(
            document_id="DOC-DAMAGE",
            claim_id=CLAIM_ID,
            document_type="damage_evidence",
            filename="initial-damage.jpg",
            received_at=NOW,
        )

        result = self.workflow.request_correction(CLAIM_ID, REVIEW_ID, "corr")

        action = self.repository.complete_human_review_resume.call_args.kwargs[
            "requested_actions"
        ][0]
        self.assertEqual(result["final_status"], "awaiting_documents")
        self.assertEqual(action["action_type"], "upload_document")
        self.assertEqual(action["document_type"], "damage_evidence")
        self.assertEqual(action["replaces_document_id"], "DOC-DAMAGE")


class HumanReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.claim_service = MagicMock()
        self.review_service = MagicMock()
        self.client = TestClient(
            create_claimant_app(
                self.claim_service,
                ["http://localhost:4200"],
                self.review_service,
            )
        )

    def test_review_api_exposes_only_token_scoped_operations(self) -> None:
        self.review_service.get_public_review.return_value = HumanReviewPublicResponse(
            review_id=REVIEW_ID,
            claim_id=CLAIM_ID,
            status="pending",
            reason=briefing().reason,
            briefing=briefing(),
            expires_at=NOW + timedelta(hours=1),
        )
        self.review_service.approve.return_value = HumanReviewDecisionResponse(
            review_id=REVIEW_ID,
            claim_id=CLAIM_ID,
            status="approved",
            event_id="approve-event",
            message="Review approved.",
        )
        self.review_service.continue_manual_handling.return_value = (
            HumanReviewDecisionResponse(
                review_id=REVIEW_ID,
                claim_id=CLAIM_ID,
                status="manual_handling",
                event_id="manual-event",
                message="Manual handling recorded.",
            )
        )

        headers = {"X-Review-Token": "secure-review-token-123"}
        self.assertEqual(
            self.client.get("/api/reviews/current", headers=headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/api/reviews/current/approve", json={}, headers=headers
            ).status_code,
            202,
        )
        self.assertEqual(
            self.client.post(
                "/api/reviews/current/manual-handling", json={}, headers=headers
            ).status_code,
            202,
        )
        self.assertEqual(self.client.patch(f"/api/claims/{CLAIM_ID}", json={}).status_code, 405)
        self.assertEqual(self.client.post("/events/pubsub", json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
