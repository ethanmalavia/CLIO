#!/usr/bin/env bash
# Build the image with Cloud Build and deploy it to Cloud Run as a private service.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require NETCDF_FILE CHROMADB_COLLECTION GEMINI_MODEL MIN_INSTANCES MAX_INSTANCES \
        DAILY_QUERY_LIMIT QUERY_RATE_PER_MINUTE REGION_RATE_PER_MINUTE \
        MAX_MESSAGE_CHARS MAX_REQUEST_BYTES

case "$GEMINI_MODEL" in
    *latest*)
        echo "GEMINI_MODEL must be a pinned model ID, not '$GEMINI_MODEL'." >&2
        exit 1
        ;;
esac

cd "$REPO_ROOT"
TAG="$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
IMAGE="$IMAGE_BASE:$TAG"
DATA="/data/$DATA_VERSION"

ENV_VARS=(
    "NETCDF_DATA_PATH=$DATA/netcdf/$NETCDF_FILE"
    "CHROMADB_SOURCE_PATH=$DATA/chromadb"
    "CHROMADB_PATH=/tmp/chromadb"
    "CHROMADB_COLLECTION=$CHROMADB_COLLECTION"
    "DOCS_PATH=$DATA/documents"
    "GEMINI_MODEL=$GEMINI_MODEL"
    "DAILY_QUERY_LIMIT=$DAILY_QUERY_LIMIT"
    "QUERY_RATE_PER_MINUTE=$QUERY_RATE_PER_MINUTE"
    "REGION_RATE_PER_MINUTE=$REGION_RATE_PER_MINUTE"
    "MAX_MESSAGE_CHARS=$MAX_MESSAGE_CHARS"
    "MAX_REQUEST_BYTES=$MAX_REQUEST_BYTES"
    "ALLOWED_ORIGINS=${ALLOWED_ORIGINS:-}"
    "NOMINATIM_USER_AGENT=${NOMINATIM_USER_AGENT:-}"
    "NOMINATIM_MIN_INTERVAL=${NOMINATIM_MIN_INTERVAL:-1.1}"
    "AUDIT_LOG=${AUDIT_LOG:-false}"
)
# "^|^" tells gcloud to split on | instead of commas: ALLOWED_ORIGINS may hold commas
# and NOMINATIM_USER_AGENT usually holds an email address.
ENV_ARG="^|^$(IFS="|"; echo "${ENV_VARS[*]}")"

echo "==> Building $IMAGE"
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --tag "$IMAGE" .

echo "==> Deploying $SERVICE (data $DATA_VERSION, $NETCDF_FILE, model $GEMINI_MODEL)"
gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$IMAGE" \
    --service-account "$SERVICE_ACCOUNT" \
    --no-allow-unauthenticated \
    --execution-environment gen2 \
    --cpu 2 --memory 4Gi \
    --concurrency 8 \
    --timeout 300 \
    --cpu-boost \
    --min-instances "$MIN_INSTANCES" \
    --max-instances "$MAX_INSTANCES" \
    --add-volume "name=data,type=cloud-storage,bucket=$BUCKET,readonly=true" \
    --add-volume-mount "volume=data,mount-path=/data" \
    --set-secrets "GEMINI_API_KEY=$SECRET_NAME:latest,GOOGLE_API_KEY=$SECRET_NAME:latest" \
    --set-env-vars "$ENV_ARG"

echo
echo "Deployed. To open it, run:"
echo "  gcloud run services proxy $SERVICE --project $PROJECT_ID --region $REGION --port 8080"
echo "then browse to http://localhost:8080"
