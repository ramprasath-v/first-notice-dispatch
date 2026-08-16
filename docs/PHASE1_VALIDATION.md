# Phase 1 Validation

The immutable Phase 1 checkpoint is tagged `phase1-stable-v1` in this repository.

Phase 1 validates FirstNotice's event-driven intake boundary, not insurance adjudication. The supported workflow ends with inspection coordination and adjuster handoff.

## Automated workflow matrix

The frozen regression matrix in `backend/tests/test_workflow_regression_matrix.py` covers the five standard automated paths:

1. Complete, consistent evidence proceeds to the inspection-decision boundary.
2. Missing routine evidence pauses at `awaiting_documents`, accepts the requested artifact, and resumes the same claim.
3. A uniquely corroborated, replaceable image outlier triggers targeted claimant remediation; a validated replacement supersedes the old artifact.
4. Unusable requested evidence remains outstanding without prematurely invoking Review; a later valid artifact resumes processing.
5. A uniquely corroborated, replaceable document outlier triggers targeted document replacement and excludes the superseded artifact from current reasoning.

Additional focused suites cover human review, durable injury signals, medical documents as human-review-only attachments, Pub/Sub redelivery, Firestore idempotency, Calendar idempotency, and Gmail notification behavior.

## Commands

Run from an activated backend virtual environment:

```bash
cd backend
python -m unittest tests.test_workflow_regression_matrix -v
python -m unittest discover -s tests -v
python -m compileall -q app *.py
python -m pip check
```

Frontend:

```bash
cd frontend
npm ci
npm test
NG_BUILD_MAX_WORKERS=1 npm run build
```

No lint target is configured. Test totals are deliberately omitted because counts change as coverage grows.

## Live integration checks

The cloud validation boundary includes:

- Angular submission to the public claimant API;
- raw evidence in private Cloud Storage and structured state in Firestore;
- authenticated Pub/Sub push to private dispatch;
- Vertex AI structured intake/review through Google ADK;
- durable missing-evidence and human-review pause/resume;
- retry-safe replacement/supersession and event processing;
- inspection creation in Google Calendar; and
- secure review and final handoff through Gmail.

Use only synthetic, rights-cleared evidence. Verify current deployment details rather than treating historical claim IDs, logs, screenshots, or test counts as canonical evidence.

## Safety assertions

Validation must continue to confirm that Gemini recommendations do not directly control Firestore transitions and that FirstNotice never approves or denies a claim, decides coverage/liability, concludes fraud, calculates payout, or makes a medical diagnosis.

See [Deployment and Reproducibility](DEPLOYMENT.md) for environment setup and [Architecture](ARCHITECTURE.md) for system boundaries.
