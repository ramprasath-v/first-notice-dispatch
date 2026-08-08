# Archived Pub/Sub Provisioning Notes

This is the original one-time setup sequence retained for historical context. It is **not idempotent**, contains deployment-specific resource names, and should not be executed as a script during the final demo.

The live topology was verified separately: `firstnotice-claim-events-push` sends OIDC-authenticated pushes to the private `firstnotice-dispatch/events/pubsub` endpoint using `firstnotice-pubsub-push@firstnotice-ai.iam.gserviceaccount.com`.

If rebuilding the project in a different Google Cloud project, review and adapt each operation individually:

```bash
PROJECT_ID='<google-cloud-project>'
REGION='<cloud-run-region>'
SERVICE_NAME='firstnotice-dispatch'
TOPIC='firstnotice-claim-events'
SUBSCRIPTION='firstnotice-claim-events-push'
PUSH_SA='firstnotice-pubsub-push'

gcloud config set project "$PROJECT_ID"
gcloud services enable pubsub.googleapis.com run.googleapis.com
gcloud pubsub topics create "$TOPIC"
gcloud iam service-accounts create "$PUSH_SA" \
  --display-name='FirstNotice Pub/Sub push identity'

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" --format='value(status.url)')
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" \
  --format='value(projectNumber)')

gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --region="$REGION" \
  --member="serviceAccount:${PUSH_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role='roles/run.invoker'

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role='roles/iam.serviceAccountTokenCreator'

gcloud pubsub subscriptions create "$SUBSCRIPTION" \
  --topic="$TOPIC" \
  --push-endpoint="${SERVICE_URL}/events/pubsub" \
  --push-auth-service-account="${PUSH_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --push-auth-token-audience="$SERVICE_URL"
```

Prefer declarative infrastructure before using this sequence beyond the hackathon.
