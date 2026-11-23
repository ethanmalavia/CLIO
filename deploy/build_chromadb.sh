#!/usr/bin/env bash
# Build the Chroma DB in Cloud Build (no local Python packages needed) and store it
# at gs://$BUCKET/$DATA_VERSION/chromadb/, next to the source PDFs it was built from.
#
# Embeds with rag/rag_manager.py's own code (see deploy/build_chromadb.py). The
# title/author/year/DOI of each document come from deploy/rag_metadata.json, which
# stands in for the interactive review.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require LOCAL_DOCS_DIR CHROMADB_COLLECTION

cd "$REPO_ROOT"
DEST="gs://$BUCKET/$DATA_VERSION"

if gcloud storage ls "$DEST/chromadb/" 2>/dev/null | grep -q .; then
    echo "$DEST/chromadb already exists. Set a new DATA_VERSION to rebuild it." >&2
    exit 1
fi
if ! ls "$LOCAL_DOCS_DIR"/*.pdf >/dev/null 2>&1; then
    echo "No PDFs in $LOCAL_DOCS_DIR. Download them as described in the main README." >&2
    exit 1
fi

# A relative path, so the Windows build of gcloud also understands it.
STAGE=".build-chromadb"
rm -rf "$STAGE"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/documents" "$STAGE/deploy"
cp -r pyproject.toml README.md src rag "$STAGE/"
cp "$LOCAL_DOCS_DIR"/*.pdf "$STAGE/documents/"
cp deploy/rag_metadata.json "$STAGE/documents/metadata.json"
cp deploy/cloudbuild-chromadb.yaml deploy/build_chromadb.py "$STAGE/deploy/"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +

echo "==> Uploading source documents to $DEST/documents/"
gcloud storage cp -n "$STAGE"/documents/* "$DEST/documents/"

echo "==> Letting Cloud Build read the Gemini key and write the Chroma DB"
BUILD_SA="$(gcloud builds get-default-service-account --project "$PROJECT_ID" \
    --format 'value(serviceAccountEmail)')"
BUILD_SA="${BUILD_SA##*/}"
gcloud secrets add-iam-policy-binding "$SECRET_NAME" --project "$PROJECT_ID" \
    --member "serviceAccount:$BUILD_SA" --role roles/secretmanager.secretAccessor >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
    --member "serviceAccount:$BUILD_SA" --role roles/storage.objectCreator >/dev/null

echo "==> Embedding with gemini-embedding-001 in Cloud Build"
gcloud builds submit "$STAGE" --project "$PROJECT_ID" --region "$REGION" \
    --config "$STAGE/deploy/cloudbuild-chromadb.yaml" \
    --substitutions "_BUCKET=$BUCKET,_DATA_VERSION=$DATA_VERSION,_COLLECTION=$CHROMADB_COLLECTION,_SECRET=$SECRET_NAME"

gcloud storage ls -r "$DEST/chromadb/"
