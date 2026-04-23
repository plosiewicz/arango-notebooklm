"""Tiny wrapper around GCP Secret Manager.

All three services pull their API tokens (Slack, Gong) from Secret
Manager rather than env vars or local .env files. This helper caches
each secret's latest value in-process so we only hit Secret Manager
on the first call per cold start.
"""
import os

from google.cloud import secretmanager

DEFAULT_PROJECT = os.environ.get('GCP_PROJECT', 'slack-notebooklm-sync')

_cache = {}
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = secretmanager.SecretManagerServiceClient()
    return _client


def get_secret(name, project=None, version='latest'):
    """Return the UTF-8 decoded payload of the named secret, cached in-process.

    `name` is the secret short name (e.g. "slack-bot-token"). The latest
    version is used by default; pass a specific version for pinning.
    """
    project = project or DEFAULT_PROJECT
    cache_key = (project, name, version)
    if cache_key in _cache:
        return _cache[cache_key]

    secret_path = f"projects/{project}/secrets/{name}/versions/{version}"
    response = _get_client().access_secret_version(name=secret_path)
    value = response.payload.data.decode('utf-8').strip()
    _cache[cache_key] = value
    return value
