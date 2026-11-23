#!/usr/bin/env bash
# Deploy on every push to GitHub: connect the repository to Cloud Build and create a
# trigger that runs deploy/cloudbuild.yaml. Safe to re-run.
#
# Needs a service that deploy.sh has already deployed once; the trigger only
# replaces its image. Settings (config.env, optional):
#   GITHUB_REPO_URL   https://github.com/<owner>/<repo>.git   (required)
#   TRIGGER_BRANCH    branch that deploys                      (default: main)
#   GITHUB_CONNECTION name of the Cloud Build connection       (default: github)
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require GITHUB_REPO_URL

BRANCH="${TRIGGER_BRANCH:-main}"
CONNECTION="${GITHUB_CONNECTION:-github}"
REPO_NAME="$(basename "$GITHUB_REPO_URL" .git | tr '[:upper:]' '[:lower:]')"
DEPLOYER_NAME="${SERVICE}-deployer"
DEPLOYER="$DEPLOYER_NAME@$PROJECT_ID.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')"
CLOUDBUILD_AGENT="service-$PROJECT_NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com"

if ! gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
    echo "Service $SERVICE does not exist yet. Run deploy/deploy.sh once first." >&2
    exit 1
fi

echo "==> Deployer service account (build, push to $REPO, deploy $SERVICE)"
if ! gcloud iam service-accounts describe "$DEPLOYER" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$DEPLOYER_NAME" --project "$PROJECT_ID" \
        --display-name "CLIO Cloud Build deployer ($SERVICE)"
fi
gcloud artifacts repositories add-iam-policy-binding "$REPO" --project "$PROJECT_ID" \
    --location "$REGION" --member "serviceAccount:$DEPLOYER" \
    --role roles/artifactregistry.writer >/dev/null
gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT_ID" \
    --region "$REGION" --member "serviceAccount:$DEPLOYER" --role roles/run.developer >/dev/null
# Deploying a revision that runs as the runtime account requires acting as it.
gcloud iam service-accounts add-iam-policy-binding "$SERVICE_ACCOUNT" --project "$PROJECT_ID" \
    --member "serviceAccount:$DEPLOYER" --role roles/iam.serviceAccountUser >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition None \
    --member "serviceAccount:$DEPLOYER" --role roles/logging.logWriter >/dev/null

echo "==> GitHub connection '$CONNECTION'"
# Cloud Build keeps the GitHub token in Secret Manager.
gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition None \
    --member "serviceAccount:$CLOUDBUILD_AGENT" --role roles/secretmanager.admin >/dev/null
if ! gcloud builds connections describe "$CONNECTION" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
    gcloud builds connections create github "$CONNECTION" --project "$PROJECT_ID" --region "$REGION"
fi
STAGE="$(gcloud builds connections describe "$CONNECTION" --project "$PROJECT_ID" \
    --region "$REGION" --format 'value(installationState.stage)')"
if [ "$STAGE" != COMPLETE ]; then
    gcloud builds connections describe "$CONNECTION" --project "$PROJECT_ID" --region "$REGION" \
        --format 'value(installationState.message,installationState.actionUri)'
    echo
    echo "Open the link above, sign in to GitHub, authorize Google Cloud Build and install"
    echo "it on the repository. Then re-run this script."
    exit 0
fi

echo "==> Repository $GITHUB_REPO_URL"
if ! gcloud builds repositories describe "$REPO_NAME" --connection "$CONNECTION" \
        --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
    gcloud builds repositories create "$REPO_NAME" --remote-uri "$GITHUB_REPO_URL" \
        --connection "$CONNECTION" --project "$PROJECT_ID" --region "$REGION"
fi

echo "==> Trigger: push to '$BRANCH' deploys $SERVICE"
TRIGGER="deploy-$SERVICE-$(echo "$BRANCH" | tr -c 'a-zA-Z0-9\n' '-')"
if ! gcloud builds triggers describe "$TRIGGER" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
    gcloud builds triggers create github --name "$TRIGGER" \
        --project "$PROJECT_ID" --region "$REGION" \
        --repository "projects/$PROJECT_ID/locations/$REGION/connections/$CONNECTION/repositories/$REPO_NAME" \
        --branch-pattern "^${BRANCH}\$" \
        --build-config deploy/cloudbuild.yaml \
        --service-account "projects/$PROJECT_ID/serviceAccounts/$DEPLOYER" \
        --substitutions "_REGION=$REGION,_REPO=$REPO,_SERVICE=$SERVICE"
fi

echo
echo "Done. Every push to '$BRANCH' on $GITHUB_REPO_URL now deploys $SERVICE."
echo "Watch builds: gcloud builds list --project $PROJECT_ID --region $REGION"
