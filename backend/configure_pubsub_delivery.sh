#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-firstnotice-ai}"
SUBSCRIPTION_ID="${PUBSUB_SUBSCRIPTION_ID:-firstnotice-claim-events-push}"

gcloud pubsub subscriptions update "$SUBSCRIPTION_ID" \
  --project="$PROJECT_ID" \
  --ack-deadline=120 \
  --min-retry-delay=10s \
  --max-retry-delay=60s
