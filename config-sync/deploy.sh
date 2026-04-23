#!/usr/bin/env bash
# Deploy the config-sync Cloud Function.
set -euo pipefail

SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/.." && pwd)"

cleanup() {
    rm -rf "$SERVICE_DIR/shared"
}
trap cleanup EXIT

rsync -a --delete "$REPO_ROOT/shared/" "$SERVICE_DIR/shared/"

cd "$SERVICE_DIR"

gcloud functions deploy config-sync \
    --project=slack-notebooklm-sync \
    --region=us-central1 \
    --gen2 \
    --runtime=python312 \
    --source=. \
    --entry-point=config_sync \
    --trigger-http \
    --allow-unauthenticated \
    --timeout=540s \
    --memory=256MB \
    "$@"
