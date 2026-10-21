#!/usr/bin/env bash
# Shared setup for the deploy scripts: load config and check required values.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEPLOY_DIR/.." && pwd)"
# One config per deployed service; pick another with CLIO_CONFIG=deploy/<name>.env
CONFIG="${CLIO_CONFIG:-$DEPLOY_DIR/config.env}"

if [ ! -f "$CONFIG" ]; then
    echo "Missing $CONFIG. Copy deploy/config.env.example to deploy/config.env and fill it in." >&2
    exit 1
fi

# Strip CRLFs and trailing comments, then export every KEY=value.
set -a
# shellcheck disable=SC1090
source <(sed -e 's/\r$//' -e 's/[[:space:]]*#.*$//' "$CONFIG")
set +a

require() {
    for name in "$@"; do
        if [ -z "${!name:-}" ]; then
            echo "$CONFIG: $name is required." >&2
            exit 1
        fi
    done
}

require PROJECT_ID REGION SERVICE REPO SERVICE_ACCOUNT_NAME SECRET_NAME BUCKET DATA_VERSION

SERVICE_ACCOUNT="$SERVICE_ACCOUNT_NAME@$PROJECT_ID.iam.gserviceaccount.com"
IMAGE_BASE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$SERVICE"
