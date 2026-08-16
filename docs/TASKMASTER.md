# Taskmaster Track Mapping

FirstNotice Dispatch is a complete first-mile workflow coordinator rather than a conversational assistant. This document maps only implemented, live-tested behavior to the Taskmaster criteria.

## Complete workflow, not a chatbot

The claimant submits one form with incident context and evidence. The backend then coordinates intake, review, evidence remediation, optional human review, inspection scheduling, and adjuster handoff. The user does not select agents, write tool instructions, or guide each internal step.

The Angular interface presents workflow state and requested action; it is not a chat interface.

## Takes action

FirstNotice performs observable external work:

- stores evidence in Cloud Storage;
- creates and updates durable Firestore claim records;
- publishes and consumes Pub/Sub lifecycle events;
- creates a real Google Calendar inspection event;
- sends a real Gmail human-review request when needed;
- accepts a secure adjuster decision; and
- sends a real Gmail adjuster handoff.

These are persisted actions with IDs and timeline events, not simulated frontend messages.

## Event-driven

Typed Pub/Sub events define the asynchronous boundaries:

- `claim.submitted`
- `claim.document.received`
- `claim.human_review.approved`
- `claim.human_review.correction_requested`
- `claim.correction.received`
- `claim.inspection.ready`

HTTP submission does not wait for Gemini or dispatch. Evidence uploads and human decisions publish new events that wake the same durable claim later. Pub/Sub pushes with OIDC to a private Cloud Run service.

## Autonomous routing

Gemini supplies structured, grounded evidence reasoning; deterministic Python rules remain authoritative. The workflow routes among:

- `awaiting_documents` for resolvable missing or unusable evidence;
- `human_review_required` for possible injury, material safety concerns, significant grounded conflicts, or consequential ambiguity that ordinary evidence collection cannot safely resolve; and
- `inspection_pending` when intake requirements are satisfied and automation may continue.

Routine missing vehicle identity or an unreadable plate does not become urgent human review. A single evidence artifact can satisfy multiple compatible internal requirements.

## Long-running and stateful

Firestore preserves:

- current claim status and validated intake/review structures;
- uploaded-document metadata and quality outcomes;
- event receipt/completion/failure attempts;
- human-review checkpoints and decision status;
- appointments and Calendar IDs;
- notifications and Gmail metadata; and
- submission, resume, dispatch, and external-effect idempotency keys.

The workflow can pause for minutes or longer, survive process termination, and resume from a later browser upload or human decision without creating another claim.

## Different applications and services

The workflow coordinates:

- Angular claimant and adjuster-review UI;
- public claimant/review FastAPI service;
- private dispatch FastAPI service;
- Cloud Storage;
- Firestore;
- Pub/Sub;
- Google ADK;
- Gemini through Vertex AI;
- Google Calendar API;
- Gmail API;
- Secret Manager; and
- Cloud Build / Artifact Registry for frontend deployment.

Each service has a bounded role; privileged event execution is not exposed through the public services.

## Human in the loop only when needed

Missing routine evidence stays in the claimant remediation path. Human review is reserved for consequential uncertainty, grounded conflict, or safety indicators.

When human review is required:

1. FirstNotice creates a durable briefing and hash-only token record.
2. Gmail sends an expiring secure link.
3. The adjuster may approve operational continuation or request a correction.
4. The single-use decision publishes a typed event.
5. The same claim resumes under the normal deterministic guards.

Human approval cannot force inspection while required evidence remains unresolved.

## Heavy lifting

After the initial claimant submission, FirstNotice:

1. uploads and inventories evidence;
2. extracts structured multimodal facts;
3. reviews quality, completeness, and conflicts;
4. applies authoritative safety and routing rules;
5. persists the stop state and explains the next claimant action;
6. observes evidence or human events;
7. validates and resumes the existing workflow;
8. emits the inspection boundary only from durable eligible state;
9. schedules a deterministic inspection;
10. generates the adjuster-ready packet; and
11. sends the final handoff.

The claimant performs only actions that genuinely require claimant evidence. The adjuster enters only at the consequential checkpoint or final handoff.

## Implementation evidence

| Claim | Repository evidence |
|---|---|
| ADK orchestration | `backend/app/agents/firstnotice_adk.py`, `backend/app/events/coordinator_invoker.py` |
| Typed event contract | `backend/app/events/claim_events.py` |
| Private event boundary and dispatch wake-up | `backend/app/events/claim_event_handler.py` |
| Durable state and idempotency | `backend/app/tools/firestore_repository.py` |
| Deterministic routing guardrails | `backend/app/services/claim_review_service.py`, `backend/app/domain/claim_status.py` |
| Missing-evidence resume | `backend/app/workflows/claim_resume_workflow.py` |
| Human checkpoint | `backend/app/services/human_review_service.py` |
| Calendar and Gmail actions | `backend/app/integrations/google_calendar_service.py`, `backend/app/integrations/gmail_service.py` |
| Dispatch workflow | `backend/app/workflows/claim_dispatch_workflow.py` |
| Workflow observability | `frontend/src/app/components/claim-timeline/` |
| Regression discipline | `backend/tests/`, Angular `*.spec.ts` files |

## Honest boundary

FirstNotice completes operational intake and adjuster handoff. It does not adjudicate the claim, determine liability or coverage, conclude fraud, calculate payout, or replace the adjuster.
