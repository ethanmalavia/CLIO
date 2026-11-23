#!/usr/bin/env bash
# Upload the NetCDF files, and a locally built Chroma DB if there is one, to
# gs://$BUCKET/$DATA_VERSION/. Without a local Chroma DB, build it in the cloud
# afterwards with deploy/build_chromadb.sh.
# Each DATA_VERSION is write-once: set a new one in config.env to publish new data.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require LOCAL_NETCDF_DIR

cd "$REPO_ROOT"
DEST="gs://$BUCKET/$DATA_VERSION"

if gcloud storage ls "$DEST/" >/dev/null 2>&1; then
    echo "$DEST already exists. Set a new DATA_VERSION in deploy/config.env to upload new data." >&2
    exit 1
fi
if ! ls "$LOCAL_NETCDF_DIR"/*.nc >/dev/null 2>&1; then
    echo "No .nc files in $LOCAL_NETCDF_DIR." >&2
    exit 1
fi

echo "==> Uploading NetCDF files to $DEST/netcdf/"
gcloud storage cp "$LOCAL_NETCDF_DIR"/*.nc "$DEST/netcdf/"

if [ -f "${LOCAL_CHROMADB_DIR:-}/chroma.sqlite3" ]; then
    echo "==> Uploading Chroma DB to $DEST/chromadb/"
    gcloud storage cp -r "$LOCAL_CHROMADB_DIR"/* "$DEST/chromadb/"
else
    echo "==> No local Chroma DB; build it with: bash deploy/build_chromadb.sh"
fi

gcloud storage ls -r "$DEST/"
