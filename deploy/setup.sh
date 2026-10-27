#!/usr/bin/env bash
# One-time setup of the Google Cloud resources CLIO needs. Safe to re-run.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require BILLING_ACCOUNT BUDGET_USD

gcloud config set project "$PROJECT_ID"

echo "==> Enabling APIs"
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    storage.googleapis.com \
    billingbudgets.googleapis.com \
    firestore.googleapis.com

echo "==> Artifact Registry repository (keeps the 5 newest images, deletes older ones after 30 days)"
if ! gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1; then
    gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"
fi
gcloud artifacts repositories set-cleanup-policies "$REPO" --location "$REGION" \
    --policy "$DEPLOY_DIR/cleanup-policy.json" --no-dry-run

echo "==> Private data bucket"
if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://$BUCKET" --location "$REGION" \
        --uniform-bucket-level-access --public-access-prevention
fi

echo "==> Runtime service account (read-only data, Gemini key only)"
if ! gcloud iam service-accounts describe "$SERVICE_ACCOUNT" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" --display-name "CLIO Cloud Run runtime"
fi
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
    --member "serviceAccount:$SERVICE_ACCOUNT" --role roles/storage.objectViewer >/dev/null

echo "==> Firestore database for daily usage counts (LIMITS_STORE=firestore)"
if ! gcloud firestore databases describe --database "(default)" >/dev/null 2>&1; then
    gcloud firestore databases create --database "(default)" --location "$REGION" --type firestore-native
fi
gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition None \
    --member "serviceAccount:$SERVICE_ACCOUNT" --role roles/datastore.user >/dev/null
# Day-old counters delete themselves (each document carries an expireAt timestamp).
for collection in clio_daily_totals clio_daily_clients; do
    gcloud firestore fields ttls update expireAt --collection-group "$collection" \
        --enable-ttl --async >/dev/null
done

echo "==> Gemini API key in Secret Manager"
if ! gcloud secrets describe "$SECRET_NAME" >/dev/null 2>&1; then
    read -r -s -p "Paste the Gemini API key (input hidden): " key
    echo
    printf '%s' "$key" | gcloud secrets create "$SECRET_NAME" --replication-policy automatic --data-file -
    unset key
fi
gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
    --member "serviceAccount:$SERVICE_ACCOUNT" --role roles/secretmanager.secretAccessor >/dev/null

echo "==> Monthly budget of \$$BUDGET_USD with email alerts at 50/90/100%"
if ! gcloud billing budgets list --billing-account "$BILLING_ACCOUNT" \
        --format 'value(displayName)' | grep -qx "CLIO $PROJECT_ID"; then
    gcloud billing budgets create \
        --billing-account "$BILLING_ACCOUNT" \
        --display-name "CLIO $PROJECT_ID" \
        --budget-amount "${BUDGET_USD}USD" \
        --filter-projects "projects/$PROJECT_ID" \
        --threshold-rule percent=0.5 \
        --threshold-rule percent=0.9 \
        --threshold-rule percent=1.0
fi

echo
echo "Setup done. Remaining manual step: cap Gemini requests per day (deploy/README.md, step 3)."
