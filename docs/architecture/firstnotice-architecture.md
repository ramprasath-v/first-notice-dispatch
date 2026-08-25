# FirstNotice Architecture

FirstNotice is an event-driven auto-insurance FNOL coordinator. A claimant submits evidence once; public Cloud Run services accept it durably; Pub/Sub wakes a private processor; Google ADK coordinates bounded Intake and Review work; Gemini returns structured reasoning; and deterministic application logic decides whether the claim can continue, must pause for more evidence, or requires human review.

## Architecture at a glance

![FirstNotice architecture](firstnotice-architecture.png)

The PNG above is the authoritative architecture diagram tracked for project documentation and submission use. No PDF artifact or matching editable vector source is currently maintained in this repository.

## Key boundaries

- **Model versus authority:** Gemini extracts and reasons; application validation and deterministic logic own consequential routing and state transitions.
- **Evidence versus metadata:** raw PDF/image evidence stays in Cloud Storage; Firestore holds durable workflow state, grounded metadata, requested actions, review generations, events/idempotency, appointments, and notifications.
- **Durable pause/resume:** claimant evidence and adjuster decisions publish events that resume the same Firestore-backed claim.
- **Human safety:** ambiguity, safety concerns, or injury signals stop autonomous continuation. Medical documents are supporting human-review material only.
- **Operational scope:** FirstNotice coordinates intake, inspection, Calendar scheduling, and Gmail handoff. It does not decide coverage, liability, fraud, payout, settlement, or medical conclusions.
