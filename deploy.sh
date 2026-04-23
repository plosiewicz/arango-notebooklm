#!/usr/bin/env bash
# Root deploy dispatcher.
#
# Usage:
#   ./deploy.sh slack
#   ./deploy.sh gong
#   ./deploy.sh config
#   ./deploy.sh all
#
# Any extra args are passed through to gcloud (e.g. --update-env-vars=...).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 {slack|gong|config|all} [extra gcloud args...]" >&2
    exit 1
fi

target="$1"
shift

deploy_one() {
    local name="$1"
    echo "--- Deploying $name ---"
    "$REPO_ROOT/$name-sync/deploy.sh" "$@"
}

case "$target" in
    slack)  deploy_one slack "$@" ;;
    gong)   deploy_one gong "$@" ;;
    config) deploy_one config "$@" ;;
    all)
        deploy_one slack "$@"
        deploy_one gong "$@"
        deploy_one config "$@"
        ;;
    *)
        echo "Unknown target: $target (expected slack|gong|config|all)" >&2
        exit 1
        ;;
esac
