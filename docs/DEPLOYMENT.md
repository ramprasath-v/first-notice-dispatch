# Deployment and Reproducibility

This guide describes the current three-service Google Cloud deployment for FirstNotice. It uses placeholders throughout: never commit project credentials, OAuth tokens, service-account keys, real claim evidence, or claimant data.

FirstNotice is an intake and operational-routing system. It does not approve or deny claims, decide coverage or liability, conclude fraud, or calculate payouts.

## 1. Deployment topology

| Service | Entry point or image | Exposure | Responsibility |
|---|---|---|---|
| `firstnotice-web` | Angular/nginx container in `frontend/` | Public | Static claimant and tokenized adjuster UI; contains only a non-secret API URL |
| `firstnotice-claimant-api` | `uvicorn claimant_main:app` from `backend/` | Public controlled-demo API | Claim submission/status, evidence upload, and tokenized review operations |
| `firstnotice-dispatch` | `uvicorn main:app` from `backend/` | Private | OIDC-authenticated Pub/Sub receiver, ADK orchestration, Vertex AI, Calendar, and Gmail |

Raw evidence is stored in Cloud Storage. Firestore stores durable workflow state and metadata. Pub/Sub carries typed identifiers and events, not evidence bytes. See [Architecture](ARCHITECTURE.md) for the detailed trust and state boundaries.

## 2. Prerequisites

- Python 3.11 or newer.
- Node.js 22 and npm 11 (matching `frontend/package.json`).
- Google Cloud CLI (`gcloud`).
- A Google Cloud project with billing enabled.
- Permission to create or administer the resources described below.
- Docker only if building containers locally. The checked-in frontend build uses Cloud Build, so local Docker is optional.
- A Google account for the Gmail sender and, when Calendar is enabled, a secondary calendar that can be shared with the dispatch runtime identity.

Authenticate the CLI and local Application Default Credentials (ADC):

```bash
export PROJECT_ID='<google-cloud-project-id>'
gcloud auth login
gcloud auth application-default login
gcloud config set project "$PROJECT_ID"
```

Local backend calls and deployed services use ADC/workload identity for Google Cloud APIs. Do not create or download a service-account JSON key.

## 3. Required Google APIs

Enable the APIs used by the application and its checked-in build path:

```bash
gcloud services enable \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  calendar-json.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  gmail.googleapis.com \
  iam.googleapis.com \
  pubsub.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com \
  --project="$PROJECT_ID"
```

Vertex AI, Firestore, Storage, Pub/Sub, Cloud Run, Calendar, Gmail, and Secret Manager are runtime dependencies. Cloud Build and Artifact Registry support the repository's frontend container build. IAM supports service-account provisioning and bindings.

## 4. Resource names

Choose names for your project rather than copying the demo deployment:

```bash
export REGION='<cloud-run-region>'
export FIRESTORE_LOCATION='<firestore-location>'
export FIRESTORE_DATABASE='<firestore-database-id>'
export EVIDENCE_BUCKET='<globally-unique-evidence-bucket>'
export CLAIM_EVENTS_TOPIC='<claim-events-topic>'
export CLAIM_EVENTS_SUBSCRIPTION='<claim-events-push-subscription>'
export RUNTIME_SA_NAME='<runtime-service-account-name>'
export PUSH_SA_NAME='<pubsub-push-service-account-name>'
export ARTIFACT_REPOSITORY='<artifact-repository-name>'

export RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
export PUSH_SA="${PUSH_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
```

Keep regions compatible with your data-residency requirements. `GOOGLE_CLOUD_LOCATION` is the configured Vertex AI location and is independent of the Cloud Run region.

## 5. Durable data and messaging

### Firestore

Create a named Firestore database in Native mode if it does not already exist:

```bash
gcloud firestore databases create \
  --database="$FIRESTORE_DATABASE" \
  --location="$FIRESTORE_LOCATION" \
  --type=firestore-native \
  --project="$PROJECT_ID"
```

Firestore is the workflow source of truth. It holds claims, structured extraction/review results, document metadata and provenance, requested actions, review generations, processed-event/idempotency records, appointments, notifications, and timeline events. It does not hold raw uploaded files.

### Cloud Storage

Create a uniform-access bucket for raw claim evidence:

```bash
gcloud storage buckets create "gs://${EVIDENCE_BUCKET}" \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --uniform-bucket-level-access
```

Use a dedicated bucket, configure retention/lifecycle controls appropriate to your environment, and never make it public. The application stores claim/document-scoped objects and persists only their metadata and `gs://` references.

### Pub/Sub

Create the topic now; create the authenticated push subscription after the private dispatch URL is known:

```bash
gcloud pubsub topics create "$CLAIM_EVENTS_TOPIC" --project="$PROJECT_ID"
```

FirstNotice assumes at-least-once delivery. Stable event IDs, Firestore reservations, and idempotent external-effect records make redelivery safe. Pub/Sub is also the outer recovery boundary for transient provider failures. Do not add retry/backoff settings merely from old troubleshooting notes; use an intentional environment-specific policy.

## 6. Service accounts and least-privilege IAM

The current deployment uses one application runtime identity for the claimant API and private dispatch, plus a dedicated Pub/Sub push identity:

```bash
gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
  --display-name='FirstNotice runtime' --project="$PROJECT_ID"
gcloud iam service-accounts create "$PUSH_SA_NAME" \
  --display-name='FirstNotice Pub/Sub push' --project="$PROJECT_ID"
```

Grant only the roles required by the code path:

| Identity | Scope | Required access |
|---|---|---|
| Runtime service account | Project | `roles/datastore.user`, `roles/aiplatform.user` |
| Runtime service account | Evidence bucket | `roles/storage.objectAdmin` because the shared identity uploads and reads evidence |
| Runtime service account | Claim-events topic | `roles/pubsub.publisher` |
| Dispatch runtime service account | Each Gmail OAuth secret | `roles/secretmanager.secretAccessor` |
| Pub/Sub push service account | Private dispatch service | `roles/run.invoker` only |
| Pub/Sub service agent | Push service account | `roles/iam.serviceAccountTokenCreator` when required for authenticated push token creation |
| Frontend runtime identity | None | The nginx service does not call Google APIs |

Example project-level runtime grants:

```bash
for role in roles/datastore.user roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$role"
done

gcloud storage buckets add-iam-policy-binding "gs://${EVIDENCE_BUCKET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role='roles/storage.objectAdmin'

gcloud pubsub topics add-iam-policy-binding "$CLAIM_EVENTS_TOPIC" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role='roles/pubsub.publisher'
```

The deployment operator additionally needs permission to build images, deploy Cloud Run services, and act as the selected runtime identity. Grant those permissions to the operator or CI identity under your organization's IAM policy; do not grant them to the runtime service account.

## 7. Runtime configuration

Start from [`backend/.env.example`](../backend/.env.example). The application reads these names:

| Variable | Required | Purpose | Public-safe example |
|---|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | Yes | ADC billing/resource project | `<project-id>` |
| `GOOGLE_CLOUD_LOCATION` | Yes | Vertex AI location | `global` |
| `GEMINI_MODEL` | Yes | Vertex AI model selected by configuration | `gemini-3.5-flash` |
| `FIRESTORE_DATABASE` | Yes | Named Firestore database | `<database-id>` |
| `PUBSUB_CLAIM_EVENTS_TOPIC` | Yes for API/dispatch | Claim lifecycle topic | `<topic-name>` |
| `GCS_CLAIM_BUCKET` | Yes for evidence upload/read | Private raw-evidence bucket | `<bucket-name>` |
| `ALLOWED_ORIGINS` | Claimant API | Comma-separated explicit web origins; `*` is rejected | `https://<web-service-host>` |
| `FIRSTNOTICE_WEB_BASE_URL` | Human review | Public web origin used to create review links | `https://<web-service-host>` |
| `HUMAN_REVIEW_TOKEN_TTL_MINUTES` | Optional | Review capability lifetime; allowed range 5–1440 | `60` |
| `GOOGLE_CALENDAR_ENABLED` | Optional | Enables real Calendar event creation | `false` |
| `GOOGLE_CALENDAR_ID` | Required when Calendar enabled | Shared secondary Calendar ID | `<secondary-calendar-id>` |
| `GMAIL_NOTIFICATION_ENABLED` | Optional | Enables Gmail review/handoff delivery | `false` |
| `ADJUSTER_EMAIL` | Required when Gmail enabled | Notification recipient | `<adjuster@example.com>` |
| `GMAIL_SENDER_EMAIL` | Required when Gmail enabled | OAuth-authorized sender | `<sender@example.com>` |
| `GMAIL_OAUTH_CLIENT_ID` | Required when Gmail enabled | OAuth client ID, injected from Secret Manager | Secret binding only |
| `GMAIL_OAUTH_CLIENT_SECRET` | Required when Gmail enabled | OAuth client secret, injected from Secret Manager | Secret binding only |
| `GMAIL_OAUTH_REFRESH_TOKEN` | Required when Gmail enabled | Offline Gmail refresh token, injected from Secret Manager | Secret binding only |
| `API_BASE_URL` | Frontend container | Public claimant API base including `/api` | `https://<claimant-api-host>/api` |

Do not set `GEMINI_API_KEY`; Vertex AI authentication uses ADC. Do not put Gmail values in `.env`, the Angular bundle, Cloud Build substitutions, or shell history.

## 8. Gmail OAuth and Secret Manager

The Gmail adapter requests only `https://www.googleapis.com/auth/gmail.send`. Use an OAuth client appropriate to your consent-screen/application setup and authorize the intended sender account for offline access.

1. Configure the OAuth consent screen and create an OAuth client in the Google Cloud project.
2. Complete an interactive authorization flow requesting the Gmail send scope and offline access.
3. Store the client ID, client secret, and refresh token in three Secret Manager resources. Suggested resource names are `firstnotice-gmail-client-id`, `firstnotice-gmail-client-secret`, and `firstnotice-gmail-refresh-token`; the actual resource names are operator-defined.
4. Grant the dispatch runtime identity `roles/secretmanager.secretAccessor` on only those secrets.
5. Bind the secret resources to `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET`, and `GMAIL_OAUTH_REFRESH_TOKEN` on `firstnotice-dispatch`.

Create secrets without placing values on the command line; for example, pipe from a protected local input into `gcloud secrets versions add ... --data-file=-`. Never commit the input files.

Use a rotatable binding such as `SECRET_NAME:latest` where your release policy permits it. When the provider revokes or rotates a refresh token, add a new secret version and deploy a revision that resolves the new version. A Cloud Run revision pinned to an expired old version will continue failing even after a newer version exists.

Refresh tokens can be revoked by the user, consent-screen changes, OAuth client changes, or provider policy. Rotate deliberately and verify one synthetic notification after rotation. Do not print tokens during diagnosis.

## 9. Google Calendar

Calendar and Gmail are independent integrations. Calendar uses the dispatch service account's ADC and the scope `https://www.googleapis.com/auth/calendar.events`; it does not use Gmail OAuth secrets.

1. Create a secondary calendar under the account intended to own demo inspection events.
2. Share that calendar with the dispatch runtime service account using **Make changes to events**.
3. Set `GOOGLE_CALENDAR_ENABLED=true` and `GOOGLE_CALENDAR_ID=<secondary-calendar-id>` only on `firstnotice-dispatch`.

The integration creates the event directly on the configured calendar. It intentionally sends no attendee invitations and uses `sendUpdates=none`. Deterministic event IDs preserve Calendar idempotency.

## 10. Deploy the backend services

Run source deployments from `backend/`. Set non-secret variables explicitly and bind Gmail secrets only to private dispatch.

```bash
cd backend

COMMON_ENV="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GEMINI_MODEL=gemini-3.5-flash,FIRESTORE_DATABASE=${FIRESTORE_DATABASE},PUBSUB_CLAIM_EVENTS_TOPIC=${CLAIM_EVENTS_TOPIC},GCS_CLAIM_BUCKET=${EVIDENCE_BUCKET}"

gcloud run deploy firstnotice-dispatch \
  --source=. \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --service-account="$RUNTIME_SA" \
  --no-allow-unauthenticated \
  --set-env-vars="$COMMON_ENV,GOOGLE_CALENDAR_ENABLED=false,GMAIL_NOTIFICATION_ENABLED=false"

gcloud run deploy firstnotice-claimant-api \
  --source=. \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --service-account="$RUNTIME_SA" \
  --allow-unauthenticated \
  --set-build-env-vars='GOOGLE_ENTRYPOINT=uvicorn claimant_main:app --host 0.0.0.0 --port 8080' \
  --set-env-vars="$COMMON_ENV,ALLOWED_ORIGINS=http://localhost:4200,FIRSTNOTICE_WEB_BASE_URL=http://localhost:4200"
```

Then capture the URLs:

```bash
export DISPATCH_URL="$(gcloud run services describe firstnotice-dispatch --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')"
export CLAIMANT_API_ORIGIN="$(gcloud run services describe firstnotice-claimant-api --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')"
export CLAIMANT_API_URL="${CLAIMANT_API_ORIGIN}/api"
```

When enabling Gmail, add secret bindings to dispatch without exposing values:

```bash
gcloud run services update firstnotice-dispatch \
  --project="$PROJECT_ID" --region="$REGION" \
  --update-env-vars="GMAIL_NOTIFICATION_ENABLED=true,ADJUSTER_EMAIL=<adjuster-email>,GMAIL_SENDER_EMAIL=<sender-email>,FIRSTNOTICE_WEB_BASE_URL=<web-origin>" \
  --update-secrets="GMAIL_OAUTH_CLIENT_ID=<client-id-secret>:latest,GMAIL_OAUTH_CLIENT_SECRET=<client-secret-secret>:latest,GMAIL_OAUTH_REFRESH_TOKEN=<refresh-token-secret>:latest"
```

Enable Calendar separately with `GOOGLE_CALENDAR_ENABLED=true` and the configured `GOOGLE_CALENDAR_ID`.

## 11. Create the authenticated push subscription

Allow only the dedicated push identity to invoke private dispatch:

```bash
gcloud run services add-iam-policy-binding firstnotice-dispatch \
  --project="$PROJECT_ID" --region="$REGION" \
  --member="serviceAccount:${PUSH_SA}" \
  --role='roles/run.invoker'

export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
gcloud iam service-accounts add-iam-policy-binding "$PUSH_SA" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role='roles/iam.serviceAccountTokenCreator'

gcloud pubsub subscriptions create "$CLAIM_EVENTS_SUBSCRIPTION" \
  --project="$PROJECT_ID" \
  --topic="$CLAIM_EVENTS_TOPIC" \
  --push-endpoint="${DISPATCH_URL}/events/pubsub" \
  --push-auth-service-account="$PUSH_SA" \
  --push-auth-token-audience="$DISPATCH_URL"
```

Do not grant `allUsers` access to `firstnotice-dispatch`. The public claimant service does not expose `/events/pubsub`.

## 12. Build and deploy the frontend

Create an Artifact Registry Docker repository once:

```bash
gcloud artifacts repositories create "$ARTIFACT_REPOSITORY" \
  --project="$PROJECT_ID" --location="$REGION" \
  --repository-format=docker
```

From `frontend/`, build through the checked-in `cloudbuild.yaml` and deploy:

```bash
cd ../frontend
export WEB_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPOSITORY}/firstnotice-web:latest"

gcloud builds submit . \
  --project="$PROJECT_ID" \
  --config=cloudbuild.yaml \
  --substitutions="_API_BASE_URL=${CLAIMANT_API_URL},_IMAGE_URL=${WEB_IMAGE}"

gcloud run deploy firstnotice-web \
  --image="$WEB_IMAGE" \
  --project="$PROJECT_ID" --region="$REGION" \
  --allow-unauthenticated \
  --set-env-vars="API_BASE_URL=${CLAIMANT_API_URL}"

export WEB_ORIGIN="$(gcloud run services describe firstnotice-web --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')"
```

Update the claimant API with the exact frontend origin and human-review link base:

```bash
gcloud run services update firstnotice-claimant-api \
  --project="$PROJECT_ID" --region="$REGION" \
  --update-env-vars="ALLOWED_ORIGINS=${WEB_ORIGIN},FIRSTNOTICE_WEB_BASE_URL=${WEB_ORIGIN}"
```

Do not use `*` for CORS. Rebuild/redeploy the frontend whenever its build-time API URL changes; the container also writes the same non-secret URL to runtime `config.js` at startup.

## 13. Local development

Install and run the browser-facing API:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Replace placeholders with your non-secret cloud resource names.
uvicorn claimant_main:app --host 127.0.0.1 --port 8080
```

In a second terminal:

```bash
cd frontend
npm ci
npm start
```

Open `http://localhost:4200`. The local claimant API still needs ADC and configured Firestore, Storage, and Pub/Sub resources. Running `claimant_main` alone does **not** reproduce the asynchronous workflow: the private dispatch processor must receive Pub/Sub pushes at a reachable authenticated URL. This repository does not provide a Pub/Sub emulator/tunnel configuration for the full end-to-end flow. Use unit tests for local deterministic verification or a controlled Google Cloud deployment for full integration testing.

## 14. Tests

Backend:

```bash
cd backend
source .venv/bin/activate
python -m unittest discover -s tests -v
python -m compileall -q app *.py
python -m pip check
```

Frozen workflow regression matrix:

```bash
cd backend
source .venv/bin/activate
python -m unittest tests.test_workflow_regression_matrix -v
```

Frontend:

```bash
cd frontend
npm ci
npm test
NG_BUILD_MAX_WORKERS=1 npm run build
```

No lint target is configured. Do not publish historical test counts as current results unless you have just rerun the commands.

## 15. Post-deploy verification

- Confirm all three Cloud Run services report Ready.
- Confirm `firstnotice-web` and `firstnotice-claimant-api` are public only if that controlled-demo exposure is intended.
- Confirm `firstnotice-dispatch` has no `allUsers` binding and the push identity has `roles/run.invoker`.
- Confirm the subscription endpoint is `${DISPATCH_URL}/events/pubsub` and uses the expected OIDC identity/audience.
- Submit only synthetic, rights-cleared evidence and confirm the API returns one claim ID.
- Confirm raw objects appear only in the private evidence bucket and structured workflow state appears in the configured Firestore database.
- Confirm the claim's Pub/Sub event is processed and redelivery does not duplicate the claim or external effects.
- Exercise one missing-evidence pause/resume and confirm the same claim continues.
- If enabled, confirm one Calendar event appears on the configured secondary calendar and one Gmail notification reaches the configured test recipient.
- Inspect the browser bundle/runtime config and Cloud Run environment metadata to confirm no OAuth values, service-account keys, or other secrets are exposed.

For a public repository, use only synthetic fixtures and review [`sample-data/README.md`](../sample-data/README.md) before publishing sample assets.
