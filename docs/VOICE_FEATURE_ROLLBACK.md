# Voice Incident Remediation Rollback

This feature branch is isolated from the known-good submission baseline.

## Baseline

- Known-good branch: `main`
- Known-good commit: `fea2a7aa1ef4c08bb0d3ed76634ea97bce659108`
- Feature branch: `feature/voice-incident-remediation`

The repository does not record an immutable deployed container image digest or an exact known-good Cloud Run revision for this commit. Discover deployed revisions before changing traffic rather than guessing.

## Local rollback

To return a local checkout to the known-good source, first preserve any work that must be retained, then run:

```bash
git checkout main
git reset --hard fea2a7aa1ef4c08bb0d3ed76634ea97bce659108
```

To abandon the feature branch after switching away from it:

```bash
git checkout main
git branch -D feature/voice-incident-remediation
```

These commands are documentation only; they must not be run automatically during feature development.

## Cloud rollback

### Method A: redeploy the known-good source commit

Use a separate clean worktree so the feature checkout is not disturbed:

```bash
git worktree add /tmp/firstnotice-known-good fea2a7aa1ef4c08bb0d3ed76634ea97bce659108
cd /tmp/firstnotice-known-good/backend

# Set the same project, region, service account, and non-secret environment
# configuration used by the current known-good deployment.
gcloud run deploy firstnotice-claimant-api \
  --source=. \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --service-account="$RUNTIME_SA" \
  --allow-unauthenticated \
  --set-build-env-vars='GOOGLE_ENTRYPOINT=uvicorn claimant_main:app --host 0.0.0.0 --port 8080' \
  --set-env-vars="$COMMON_ENV,ALLOWED_ORIGINS=${WEB_ORIGIN},FIRSTNOTICE_WEB_BASE_URL=${WEB_ORIGIN}"

cd ../frontend
export WEB_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPOSITORY}/firstnotice-web:known-good-fea2a7a"
gcloud builds submit . \
  --project="$PROJECT_ID" \
  --config=cloudbuild.yaml \
  --substitutions="_API_BASE_URL=${CLAIMANT_API_URL},_IMAGE_URL=${WEB_IMAGE}"
gcloud run deploy firstnotice-web \
  --image="$WEB_IMAGE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --allow-unauthenticated \
  --set-env-vars="API_BASE_URL=${CLAIMANT_API_URL}"
```

If voice processing changes private dispatch code, redeploy `firstnotice-dispatch` from the same known-good backend worktree using the existing command and secret bindings in `docs/DEPLOYMENT.md`. Do not recreate or expose secrets in shell history.

### Method B: move traffic to a known-good Cloud Run revision

List revisions and inspect creation time/image digest before selecting a target:

```bash
gcloud run revisions list \
  --service=firstnotice-claimant-api \
  --project="$PROJECT_ID" \
  --region="$REGION"

gcloud run services update-traffic firstnotice-claimant-api \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --to-revisions="<known-good-claimant-api-revision>=100"

gcloud run revisions list \
  --service=firstnotice-web \
  --project="$PROJECT_ID" \
  --region="$REGION"

gcloud run services update-traffic firstnotice-web \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --to-revisions="<known-good-web-revision>=100"
```

Repeat the revision-list and traffic command for `firstnotice-dispatch` only if that service was changed and deployed by this feature. Do not execute traffic changes without confirming the selected revision.

## Expected deployment scope

- `firstnotice-web`: required for the recording UI.
- `firstnotice-claimant-api`: required if it accepts or stores the voice remediation upload.
- `firstnotice-dispatch`: required only if the feature adds voice extraction or injury-signal handling to the private workflow processor.

See `docs/DEPLOYMENT.md` for the complete environment, IAM, CORS, secret-binding, and private-dispatch requirements.
