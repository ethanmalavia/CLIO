#!/bin/sh
# Container entrypoint for Cloud Run.
#
# Chroma keeps its index in SQLite, which needs a writable local filesystem,
# so the read-only copy on the Cloud Storage mount is copied to /tmp first.
# The NetCDF file is read straight from the mount (xarray opens it lazily).
set -eu

if [ -n "${CHROMADB_SOURCE_PATH:-}" ]; then
    rm -rf /tmp/chromadb
    cp -r "$CHROMADB_SOURCE_PATH" /tmp/chromadb
    export CHROMADB_PATH=/tmp/chromadb
fi

exec gunicorn \
    --bind ":${PORT:-8080}" \
    --workers 1 \
    --threads 8 \
    --timeout 300 \
    --access-logfile - \
    clio.wsgi:app
