# FirstNotice Dispatch Demo Operator Guide

This is the practical runbook for recording or presenting the working deployment. It is not a provisioning guide.

## Demo preparation

### Set operator-only URLs

Use environment variables rather than editing URLs into this document:

```bash
export FIRSTNOTICE_WEB_URL='https://<firstnotice-web-url>'
export CLAIMANT_API_URL='https://<firstnotice-claimant-api-url>/api'
export DISPATCH_URL='https://<private-firstnotice-dispatch-url>'
```

### Pre-open these tabs

1. `$FIRSTNOTICE_WEB_URL`
2. `firstnotice.adjuster@gmail.com` inbox
3. **FirstNotice Demo Inspections** Google Calendar while signed in to `firstnotice.adjuster@gmail.com`
4. Google Cloud Console → Cloud Run → `firstnotice-dispatch` → Logs (troubleshooting only)
5. A second browser tab reserved for the secure adjuster-review link

Keep email previews and Cloud logs out of the recording until needed; they can expose irrelevant metadata.

The Calendar should be the secondary calendar owned by the dedicated adjuster account and shared with `firstnotice-runtime@firstnotice-ai.iam.gserviceaccount.com` using **Make changes to events**. Calendar records the inspection directly; Gmail separately delivers review and handoff messages. No Calendar attendee or invitation is expected.

### Prepare rights-cleared assets

Repository asset status:

| Asset | Purpose | Release status |
|---|---|---|
| `sample-data/police-report.pdf` | Synthetic report with `POL-DEMO-1001`, no injuries, drivable vehicle | Safe synthetic fixture |
| `sample-data/vehicle-photo.jpg` | Damage photo with no readable plate/identity | No visible PII; confirm redistribution rights before GitHub push |
| `sample-data/accident-photo.jpg` | Previously used damage/plate image | **Do not commit or use** until provenance is confirmed; it contains a real-looking California plate and is ignored |

Before recording, prepare two local, rights-cleared synthetic JPEG/PNG files that are not based on the excluded image:

```text
<DEMO_CLEAR_PLATE_PHOTO>       clear fictional plate; used to remediate Scenario A
<DEMO_IDENTIFIED_DAMAGE_PHOTO> damage plus readable fictional identity; used for Scenario B
```

Validate these assets once against the live workflow before recording. Do not use real policy documents, names, addresses, VINs, plates, or personal photos.

### Verify deployed services

These are read-only checks:

```bash
gcloud run services list --project=firstnotice-ai --region=us-central1 \
  --filter='metadata.name:firstnotice' \
  --format='table(metadata.name,status.url,status.conditions[0].status)'

gcloud run services get-iam-policy firstnotice-dispatch \
  --project=firstnotice-ai --region=us-central1 \
  --flatten='bindings[].members' --filter='bindings.members:allUsers'

gcloud pubsub subscriptions describe firstnotice-claim-events-push \
  --project=firstnotice-ai \
  --format='yaml(pushConfig.pushEndpoint,pushConfig.oidcToken.serviceAccountEmail)'
```

Expected:

- All three Cloud Run services are ready.
- The dispatch IAM query prints no `allUsers` member.
- The subscription targets `firstnotice-dispatch/.../events/pubsub` with the dedicated push service account.

Send no manual Pub/Sub events during the final demo.

## Scenario A — main demo: missing evidence

### Inputs

- Incident description: `My vehicle was struck while parked. The rear side panel is damaged. No injuries were reported and the vehicle is drivable.`
- Policy number hint: leave blank
- Damage photo: `sample-data/vehicle-photo.jpg`
- Police report: `sample-data/police-report.pdf` (optional for this scenario)
- Voice note: none
- Remediation upload: `<DEMO_CLEAR_PLATE_PHOTO>`

### Steps and expected UI

1. Open the claimant submission page.
2. Enter the incident description, attach the damage photo, optionally attach the synthetic report, and submit.
3. The page immediately navigates to `/claims/{claimId}`; do not wait on the form.
4. Observe **Claim received** then **Analyzing your claim**.
5. The claim reaches **More information needed**.
   - Stepper: Received ✓; Analyzed active with **Waiting for information**; Inspection and Adjuster upcoming.
   - Claim Timeline: **Additional information requested**.
   - Agent Activity: Review Agent detected missing evidence; Workflow paused.
6. Confirm the claimant sees one consolidated license-plate upload request—not separate controls for vehicle identity and plate.
7. Upload `<DEMO_CLEAR_PLATE_PHOTO>` once.
8. Do **not** press Continue or manually publish an event.
9. Observe **Document received. Rechecking your claim…** and Analyzed → **Rechecking evidence**.
10. Agent Activity should add evidence received, quality checked, requirement accepted, and automatic resume events.
11. The status advances automatically to **Preparing inspection**, then **Inspection scheduled**, then **Ready for adjuster review**.
12. Show the real event in **FirstNotice Demo Inspections** Calendar.
13. Show the final handoff in `firstnotice.adjuster@gmail.com`.
14. Return to the claimant page:
    - Stepper: Received ✓, Analyzed ✓, Inspection ✓, Adjuster ✓.
    - Agent Activity: Google Calendar scheduling and Gmail handoff appear only after their confirmed events.

## Scenario B — human review

Use a rights-cleared image that already contains sufficient damage and readable fictional vehicle identity. This isolates the policy conflict from routine missing evidence.

### Inputs

- Incident description: `My vehicle was struck from behind while stopped. No injuries were reported and it remains drivable.`
- Policy number hint: `POL-DEMO-9999`
- Damage/identity photo: `<DEMO_IDENTIFIED_DAMAGE_PHOTO>`
- Police report: `sample-data/police-report.pdf` (contains `POL-DEMO-1001`)
- Voice note: none

### Steps and expected UI

1. Submit the claim with the exact conflicting policy values above.
2. Observe intake and review without manually intervening.
3. The claim reaches **Additional review required**.
   - Stepper: Received ✓; Analyzed active with **Human review required**.
   - Claim Timeline: **Additional review required**.
   - Agent Activity: Review Agent detected conflicting policy information; Workflow paused.
4. Open `firstnotice.adjuster@gmail.com` and show the secure review request.
5. Open its review link in the prepared second browser tab.
6. Confirm the briefing contains the policy conflict and operational-only safety boundary.
7. Click **Approve & Continue**.
8. Return to the already-open claimant status page; do not refresh.
9. Polling should show **Human approval received** and **Automatically resumed the claim after human review**.
10. The stepper automatically advances to Inspection.
11. Show the new Calendar inspection and final Gmail handoff.
12. Confirm the claimant page reaches **Ready for adjuster review** and all four major steps are complete.

If approval legitimately reveals an unresolved ordinary evidence requirement, the correct result is `awaiting_documents`, not forced inspection. Fix the demo fixture rather than bypassing that guardrail.

## Recovery and troubleshooting

Keep troubleshooting out of the primary recording.

- **Stuck at Preparing inspection:** inspect the claim timeline for `claim.inspection.ready` receipt and `pubsub_event_failed`; then inspect private dispatch logs.
- **No Gmail:** look for `human_review_email_sent`, `adjuster_email_sent`, or `adjuster_email_failed`. Verify Secret Manager bindings on private dispatch without printing values.
- **No Calendar event:** inspect `GoogleCalendarError`, Calendar sharing for the runtime identity, and the deterministic event record.
- **UI appears stale:** confirm claimant API requests are succeeding. A hard refresh is acceptable only while troubleshooting, not as part of the demonstrated flow.
- **Evidence remains unresolved:** use a clear supported JPEG/PNG and verify it contains a readable fictional plate; never force the next event.
- **Provider retry:** wait for Pub/Sub retry and rely on persisted idempotency. Do not manually republish during the final demo.

## Cleanup

- Delete demo inspection events individually from **FirstNotice Demo Inspections** Calendar after noting their claim IDs.
- Delete demo Gmail messages using the demo inbox’s normal trash flow if desired.
- Retain claim records for judge evidence until submission is complete. If cleanup is required afterward, target the exact claim ID and its exact GCS prefix; never bulk-delete the project bucket or Firestore database.
- Remove local synthetic plate assets if their reuse rights do not permit publication.
- Do not delete deployed services or shared infrastructure before judging finishes.
