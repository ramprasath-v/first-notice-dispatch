# FirstNotice Dispatch claimant frontend

Angular 22 standalone application for claim submission, live claim status, missing-evidence upload, claimant timeline, judge-facing Agent Activity, and tokenized adjuster review.

The frontend contains no credentials. Local development uses `http://localhost:8080`; the production nginx container generates `config.js` from the non-secret `API_BASE_URL` environment variable.

## Develop

```bash
npm ci
npm start
```

Open `http://localhost:4200` with `claimant_main:app` running on port 8080.

## Validate

```bash
npm test
NG_BUILD_MAX_WORKERS=1 npm run build
```

There is no configured lint or end-to-end test target.

## Production container

The multi-stage [`Dockerfile`](Dockerfile) builds Angular with Node and serves `dist/frontend/browser` using nginx on port 8080. nginx:

- supports Angular route fallback;
- disables access logging so hash-route review capabilities are not accidentally logged;
- sets basic content/referrer headers;
- serves runtime config with `no-store`; and
- returns 404 for `/api/` and `/events/` so the static service cannot proxy privileged endpoints.

Build and publish with Cloud Build:

```bash
API_BASE_URL='https://<firstnotice-claimant-api-url>/api'
IMAGE_URL='us-central1-docker.pkg.dev/firstnotice-ai/firstnotice-containers/firstnotice-web:latest'

gcloud builds submit . --config=cloudbuild.yaml \
  --substitutions="_API_BASE_URL=$API_BASE_URL,_IMAGE_URL=$IMAGE_URL"
```

See the repository [README](../README.md), [architecture](../docs/ARCHITECTURE.md), and [demo guide](../DEMO.md) for the full system context.
