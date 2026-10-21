#!/usr/bin/env bash
# Upload the NetCDF files and the pre-built Chroma DB to gs://$BUCKET/$DATA_VERSION/.
# Each DATA_VERSION is write-once: set a new one in config.env to publish new data.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require LOCAL_NETCDF_DIR LOCAL_CHROMADB_DIR

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
if [ ! -f "$LOCAL_CHROMADB_DIR/chroma.sqlite3" ]; then
    echo "No Chroma DB in $LOCAL_CHROMADB_DIR. Build it first: python rag/rag_manager.py, then 'add'." >&2
    exit 1
fi

echo "==> Uploading NetCDF files to $DEST/netcdf/"
gcloud storage cp "$LOCAL_NETCDF_DIR"/*.nc "$DEST/netcdf/"

echo "==> Uploading Chroma DB to $DEST/chromadb/"
gcloud storage cp -r "$LOCAL_CHROMADB_DIR"/* "$DEST/chromadb/"

gcloud storage ls -r "$DEST/"
