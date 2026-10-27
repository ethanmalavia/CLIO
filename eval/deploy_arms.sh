#!/usr/bin/env bash
# Deploy two private evaluation services from one image, identical except for the model:
#   clio-eval-pro    GEMINI_MODEL=$1
#   clio-eval-flash  GEMINI_MODEL=$2
# Both read deploy/config.env (or $CLIO_CONFIG) for the project, bucket and data.
#
#   bash eval/deploy_arms.sh <pro-model-id> <flash-model-id>
set -euo pipefail
if [ $# -ne 2 ]; then
    echo "Usage: bash eval/deploy_arms.sh <pro-model-id> <flash-model-id>" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_CONFIG="${CLIO_CONFIG:-$REPO_ROOT/deploy/config.env}"
source "$REPO_ROOT/deploy/common.sh"

cd "$REPO_ROOT"
export IMAGE="$IMAGE_BASE:eval-$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
echo "==> Building $IMAGE"
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --tag "$IMAGE" .

deploy_arm() {
    local arm="$1" model="$2" config="deploy/eval-$1.env"
    # Later assignments win when deploy/common.sh sources the file.
    {
        cat "$BASE_CONFIG"
        echo
        echo "SERVICE=clio-eval-$arm"
        echo "GEMINI_MODEL=$model"
        echo "MIN_INSTANCES=0"
        echo "MAX_INSTANCES=1"
        echo "AUDIT_LOG=true"
        echo "QUERY_RATE_PER_MINUTE=30"
        echo "REGION_RATE_PER_MINUTE=30"
        echo "DAILY_QUERY_LIMIT=300"
        echo "NOMINATIM_MIN_INTERVAL=2.2"  # two services share Nominatim's 1 request/second
    } > "$config"
    CLIO_CONFIG="$config" bash deploy/deploy.sh
}

deploy_arm pro "$1"
deploy_arm flash "$2"

echo
echo "Both arms deployed. Run the comparison with:"
for arm in pro flash; do
    url="$(gcloud run services describe "clio-eval-$arm" --project "$PROJECT_ID" \
        --region "$REGION" --format 'value(status.url)')"
    echo "  --arm $arm=$url \\"
done
echo "(as arguments to: python eval/run_eval.py)"
