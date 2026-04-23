#!/usr/bin/env bash
# Deploy the slack-sync Cloud Function.
#
# Copies the repo-root shared/ module into this directory (so gcloud
# uploads it alongside main.py), then deploys. The local copy is
# .gitignored and cleaned up on exit.
set -euo pipefail

SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/.." && pwd)"

cleanup() {
    rm -rf "$SERVICE_DIR/shared"
}
trap cleanup EXIT

rsync -a --delete "$REPO_ROOT/shared/" "$SERVICE_DIR/shared/"

cd "$SERVICE_DIR"

gcloud functions deploy slack-sync \
    --project=slack-notebooklm-sync \
    --region=us-central1 \
    --gen2 \
    --runtime=python312 \
    --source=. \
    --entry-point=slack_webhook \
    --trigger-http \
    --allow-unauthenticated \
    --timeout=60s \
    --memory=256MB \
    "$@"
