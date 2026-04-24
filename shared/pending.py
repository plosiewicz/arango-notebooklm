"""GCS-backed FIFO buffer for calls/messages that hit a doc-cap wall.

When `shared.google_docs.append_to_doc` raises `DocFullError` we don't
want to drop the call/message - we serialize the formatted block to
GCS under a per-customer prefix and let a later run drain it once the
operator has extended the doc list.

Layout:

    gs://<bucket>/<prefix>/<partition>/<sortable-key>.json

* prefix:    `pending-calls` (gong) or `pending-messages` (slack)
* partition: customer email-domain for gong, channel id for slack
* key:       a sortable filename so `drain` returns items in order

The payload is a JSON object: `{"id": <unique>, "content": <doc text>,
"meta": {...}}`. The drain helper yields these dicts in lexicographic
key order; callers append `content` to the doc and on success call
`delete(prefix, partition, key)`.

Empty partitions are silently absent (GCS doesn't carry directory
markers); `count` returns 0 and `drain` yields nothing.
"""
import json
import os
import time
import uuid

from google.cloud import storage

DEFAULT_BUCKET = os.environ.get('CONFIG_BUCKET', 'slack-notebooklm-config')

PREFIX_GONG = 'pending-calls'
PREFIX_SLACK = 'pending-messages'

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def _key(unique_id):
    """Return a lexicographically sortable filename.

    Format: `<unix-millis>-<uuid>.json`. Two enqueues in the same
    millisecond keep order via the uuid suffix; downstream drains
    don't depend on strict ordering between same-millisecond enqueues.
    """
    if unique_id is None:
        unique_id = uuid.uuid4().hex
    millis = int(time.time() * 1000)
    return f"{millis:013d}-{unique_id}.json"


def _blob_path(prefix, partition, key):
    return f"{prefix}/{partition}/{key}"


def enqueue(prefix, partition, content, meta=None, unique_id=None, bucket=None):
    """Persist a single pending item to GCS. Returns the storage key.

    `unique_id` is recommended (e.g. the call id or message ts) so a
    crashed run that retries the enqueue doesn't double-buffer; the
    upload is `if_generation_match=0` (no overwrite) and a duplicate
    enqueue raises `google.api_core.exceptions.PreconditionFailed`,
    which callers can treat as "already pending, fine".
    """
    bucket = bucket or DEFAULT_BUCKET
    key = _key(unique_id)
    payload = json.dumps({
        'id': unique_id,
        'content': content,
        'meta': meta or {},
    })
    client = _get_client()
    blob = client.bucket(bucket).blob(_blob_path(prefix, partition, key))
    blob.upload_from_string(payload, content_type='application/json')
    return key


def list_partitions(prefix, bucket=None):
    """Return the set of partition names that currently hold items.

    Used by drain workers to find which customers/channels need
    attention without round-tripping the full sheet.
    """
    bucket = bucket or DEFAULT_BUCKET
    client = _get_client()
    iterator = client.list_blobs(bucket, prefix=f"{prefix}/")
    partitions = set()
    for blob in iterator:
        # blob.name is like "pending-calls/acme.com/0001234-uuid.json"
        rest = blob.name[len(prefix) + 1:]
        if '/' not in rest:
            continue
        partitions.add(rest.split('/', 1)[0])
    return partitions


def count(prefix, partition, bucket=None):
    """Return the number of pending items in a single partition."""
    bucket = bucket or DEFAULT_BUCKET
    client = _get_client()
    iterator = client.list_blobs(bucket, prefix=f"{prefix}/{partition}/")
    return sum(1 for _ in iterator)


def drain(prefix, partition, bucket=None):
    """Yield (key, payload_dict) for every pending item in `partition`.

    Items are yielded in lexicographic key order (i.e. enqueue order
    within millisecond resolution). Caller is responsible for calling
    `delete(prefix, partition, key)` after a successful append. If the
    caller stops iteration mid-drain the un-yielded items remain in
    GCS for the next run.
    """
    bucket = bucket or DEFAULT_BUCKET
    client = _get_client()
    blobs = sorted(
        client.list_blobs(bucket, prefix=f"{prefix}/{partition}/"),
        key=lambda b: b.name,
    )
    for blob in blobs:
        key = blob.name.rsplit('/', 1)[-1]
        try:
            payload = json.loads(blob.download_as_text())
        except Exception as e:
            print(f"Skipping unreadable pending blob {blob.name}: {e}")
            continue
        yield key, payload


def delete(prefix, partition, key, bucket=None):
    """Remove a single pending item after it's been successfully drained."""
    bucket = bucket or DEFAULT_BUCKET
    client = _get_client()
    blob = client.bucket(bucket).blob(_blob_path(prefix, partition, key))
    try:
        blob.delete()
    except Exception as e:
        # A 404 here is benign - either someone else drained it or
        # it was already deleted in a prior partial run.
        print(f"delete pending {blob.name}: {e}")
