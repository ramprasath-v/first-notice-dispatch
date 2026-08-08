# Three-Minute Demo Script

Use the prepared claims and tabs from [`DEMO.md`](../DEMO.md). Keep the main missing-evidence flow live; use a pre-prepared human-review claim so email timing does not consume the recording.

## 0:00–0:20 — Problem and pitch

**On screen:** FirstNotice submission page.

**Narration:**

“Insurance intake is not just extracting fields. Evidence arrives incomplete, conflicts need the right guardrail, and work has to resume across people and systems. FirstNotice Dispatch is an event-driven coordinator for that first mile. A claimant starts once; the system reasons, pauses safely, resumes from external events, schedules the inspection, and hands the claim to an adjuster.”

## 0:20–1:40 — Missing-evidence workflow

**On screen:** Submit Scenario A using the rights-cleared damage photo without visible identity.

**Narration:**

“I’ll submit a short description, a damage image, and our synthetic police report. The API immediately returns a claim ID; multimodal processing continues asynchronously.”

**On screen:** Claim Status moves through Claim received and analysis to More information needed. Point to the stepper and Agent Activity.

“Gemini on Vertex AI analyzed the image and PDF into validated schemas. The review found useful damage evidence, but no readable vehicle identity. Deterministic routing keeps this as a routine evidence request—not urgent human review. Firestore holds the durable pause, and the claimant sees one physical upload request because a single plate photo can satisfy both internal requirements.”

**On screen:** Upload the prepared clear fictional plate image once.

“I upload the missing evidence once. There is no Continue button and no manual workflow command.”

**On screen:** Rechecking evidence; Agent Activity adds evidence and resume events; status advances to Preparing inspection.

“The upload publishes `claim.document.received`. The private processor validates the new artifact, marks compatible requirements satisfied, and automatically resumes the same claim. When durable state reaches inspection pending, the centralized boundary emits the deterministic inspection-ready event.”

## 1:40–2:05 — Real external actions

**On screen:** Google Calendar event, then final Gmail handoff.

**Narration:**

“Dispatch selects the slot deterministically and creates this real event in Google Calendar. Gmail separately sends the adjuster-ready handoff. Stable appointment, Calendar, notification, and event IDs protect retries from duplicating effective work.”

**On screen:** Return to claimant page at Ready for adjuster review.

“The claimant page polls the public API and updates without a refresh. Adjuster notified means FirstNotice’s intake orchestration is complete—it does not mean the insurance claim was approved.”

## 2:05–2:40 — Human-review guardrail

**On screen:** Pre-prepared Scenario B claim at Additional review required, then the review Gmail.

**Narration:**

“Here is the second durable stop. The claimant’s policy hint conflicts with the synthetic police report. That consequential conflict pauses automation and sends a secure Gmail review request.”

**On screen:** Open secure review page; show briefing; click Approve & Continue.

“The token is expiring, single-use, and stored only as a hash. The adjuster approves operational continuation—not coverage, liability, or payout.”

**On screen:** Return to claimant page as it advances automatically.

“That decision publishes a human-review approval event. The same claim resumes, reaches inspection eligibility, and uses the same Calendar and Gmail dispatch path.”

## 2:40–3:00 — Observability, architecture, close

**On screen:** Agent Activity, then README architecture diagram.

**Narration:**

“Agent Activity is derived from persisted events, so judges can distinguish Gemini reasoning, deterministic workflow decisions, human action, and external systems. Underneath, a public Angular and claimant API feed Cloud Storage, Firestore, and Pub/Sub; an OIDC push wakes a private Cloud Run processor using Google ADK and Vertex AI. FirstNotice’s value is coordinated, durable heavy lifting—with automation where safe and a human exactly where needed.”

## Recording guardrails

- Do not say FirstNotice approves claims, replaces adjusters, detects fraud conclusively, decides coverage, or calculates payout.
- Do not expose review tokens, OAuth values, raw police-report contents beyond the synthetic fixture, or Cloud logs containing request metadata.
- Do not manually publish events to rescue the recorded workflow.
- If a provider is slow, cut to the prepared success tab rather than implying a false event.
