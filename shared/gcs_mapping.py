"""GCS-backed JSON mapping loader with a short in-memory cache.

Both slack-sync and gong-sync read a JSON mapping (channel-id -> docId,
or account-key -> docId) out of the same config bucket. config-sync is
the only writer. We cache for 5 minutes per blob so hot-path requests
don't hit GCS on every invocation.
"""
import json
import os
import time

from google.cloud import storage

DEFAULT_BUCKET = os.environ.get('CONFIG_BUCKET', 'slack-notebooklm-config')
CACHE_TTL_SECONDS = 300

_cache = {}  # {(bucket, blob): (loaded_at, mapping)}
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def load_mapping(blob_name, bucket=None):
    """Return the parsed JSON mapping at gs://<bucket>/<blob_name>.

    Caches each (bucket, blob) for CACHE_TTL_SECONDS. On error, returns
    the stale cache if we have one, otherwise an empty dict - callers
    treat an empty mapping as "no routes configured, skip".
    """
    bucket = bucket or DEFAULT_BUCKET
    key = (bucket, blob_name)
    now = time.time()

    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        gcs_bucket = _get_client().bucket(bucket)
        blob = gcs_bucket.blob(blob_name)
        mapping = json.loads(blob.download_as_text())
        _cache[key] = (now, mapping)
        print(f"Loaded gs://{bucket}/{blob_name} ({len(mapping)} entries)")
        return mapping
    except Exception as e:
        print(f"Error loading gs://{bucket}/{blob_name}: {e}")
        if cached:
            print("Falling back to stale cache")
            return cached[1]
        return {}


def save_mapping(blob_name, mapping, bucket=None):
    """Write a JSON mapping to gs://<bucket>/<blob_name> and refresh cache.

    Only config-sync should call this - the sync services are read-only
    against the mapping bucket.
    """
    bucket = bucket or DEFAULT_BUCKET
    gcs_bucket = _get_client().bucket(bucket)
    blob = gcs_bucket.blob(blob_name)
    blob.upload_from_string(
        json.dumps(mapping, indent=2),
        content_type='application/json',
    )
    _cache[(bucket, blob_name)] = (time.time(), mapping)
    print(f"Uploaded gs://{bucket}/{blob_name} ({len(mapping)} entries)")
