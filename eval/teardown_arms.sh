#!/usr/bin/env bash
# Delete the two evaluation services. Data, image and secret are kept.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/deploy/common.sh"

for arm in pro flash; do
    gcloud run services delete "clio-eval-$arm" --project "$PROJECT_ID" --region "$REGION" --quiet || true
    rm -f "$REPO_ROOT/deploy/eval-$arm.env"
done
