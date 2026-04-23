"""Tests for shared/gcs_mapping.py - the GCS-backed mapping cache.

Covers:
  * load_mapping returns parsed JSON on first call, cached on second
  * cache invalidates after CACHE_TTL_SECONDS
  * GCS error with prior cache -> returns stale cache
  * GCS error with no cache -> returns {}
  * save_mapping writes JSON and refreshes cache
"""
import json
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

import shared.gcs_mapping as gcs_mapping


def _fake_client(payload_or_exc):
    """Build a fake storage.Client whose download_as_text returns `payload` or raises."""
    client = MagicMock()
    bucket = MagicMock()
    blob = MagicMock()
    client.bucket.return_value = bucket
    bucket.blob.return_value = blob
    if isinstance(payload_or_exc, Exception):
        blob.download_as_text.side_effect = payload_or_exc
    else:
        blob.download_as_text.return_value = payload_or_exc
    return client, blob


def test_load_mapping_parses_and_caches(monkeypatch):
    client, _ = _fake_client(json.dumps({"C1": {"docId": "doc-abc"}}))
    monkeypatch.setattr(gcs_mapping, "_get_client", lambda: client)

    first = gcs_mapping.load_mapping("channel-mapping.json", bucket="b")
    second = gcs_mapping.load_mapping("channel-mapping.json", bucket="b")

    assert first == {"C1": {"docId": "doc-abc"}}
    assert second is first  # same object -> cache hit, no re-parse
    assert client.bucket.call_count == 1  # only one fetch


def test_load_mapping_invalidates_after_ttl(monkeypatch):
    payloads = iter([
        json.dumps({"C1": "v1"}),
        json.dumps({"C1": "v2"}),
    ])

    def fake_client_factory():
        client = MagicMock()
        bucket = MagicMock()
        blob = MagicMock()
        client.bucket.return_value = bucket
        bucket.blob.return_value = blob
        blob.download_as_text.side_effect = lambda: next(payloads)
        return client

    client = fake_client_factory()
    monkeypatch.setattr(gcs_mapping, "_get_client", lambda: client)

    with freeze_time("2025-01-01 00:00:00") as frozen:
        first = gcs_mapping.load_mapping("m.json", bucket="b")
        assert first == {"C1": "v1"}

        frozen.tick(delta=gcs_mapping.CACHE_TTL_SECONDS + 1)
        second = gcs_mapping.load_mapping("m.json", bucket="b")

    assert second == {"C1": "v2"}
    assert client.bucket.call_count == 2


def test_load_mapping_returns_stale_on_gcs_error(monkeypatch):
    """First call succeeds and populates cache. Second call errors and must fall back."""
    payloads = iter([json.dumps({"C1": "v1"})])
    client = MagicMock()
    bucket = MagicMock()
    blob = MagicMock()
    client.bucket.return_value = bucket
    bucket.blob.return_value = blob

    def flaky_download():
        try:
            return next(payloads)
        except StopIteration:
            raise RuntimeError("simulated GCS outage")

    blob.download_as_text.side_effect = flaky_download
    monkeypatch.setattr(gcs_mapping, "_get_client", lambda: client)

    with freeze_time("2025-01-01 00:00:00") as frozen:
        first = gcs_mapping.load_mapping("m.json", bucket="b")
        assert first == {"C1": "v1"}

        frozen.tick(delta=gcs_mapping.CACHE_TTL_SECONDS + 1)
        fallback = gcs_mapping.load_mapping("m.json", bucket="b")

    assert fallback == {"C1": "v1"}  # stale cache served


def test_load_mapping_returns_empty_on_gcs_error_with_no_cache(monkeypatch):
    client, _ = _fake_client(RuntimeError("nope"))
    monkeypatch.setattr(gcs_mapping, "_get_client", lambda: client)

    result = gcs_mapping.load_mapping("m.json", bucket="b")
    assert result == {}


def test_save_mapping_writes_and_refreshes_cache(monkeypatch):
    client = MagicMock()
    bucket = MagicMock()
    blob = MagicMock()
    client.bucket.return_value = bucket
    bucket.blob.return_value = blob
    monkeypatch.setattr(gcs_mapping, "_get_client", lambda: client)

    mapping = {"acme.com": {"docId": "doc-123"}}
    gcs_mapping.save_mapping("account-mapping.json", mapping, bucket="b")

    blob.upload_from_string.assert_called_once()
    uploaded_text, = blob.upload_from_string.call_args.args
    assert json.loads(uploaded_text) == mapping

    cached = gcs_mapping.load_mapping("account-mapping.json", bucket="b")
    assert cached == mapping
    assert bucket.blob.call_count == 1  # load hit the cache, did not refetch
