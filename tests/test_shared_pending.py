"""Tests for shared/pending.py - GCS-backed FIFO buffer.

Covers:
  * enqueue / list_partitions / count / drain / delete round-trip
  * lexicographic ordering of drained items
  * empty partitions
  * delete is idempotent (no-op on already-gone)

We don't mock GCS at the SDK level - we substitute a tiny in-memory
fake on `_get_client` because the SDK's MockBucket dance is heavier
than the contract we're testing.
"""
import json

import shared.pending as pending


class _FakeBlob:
    def __init__(self, bucket, name):
        self._bucket = bucket
        self.name = name

    def upload_from_string(self, data, content_type=None):
        self._bucket._store[self.name] = data

    def download_as_text(self):
        return self._bucket._store[self.name]

    def delete(self):
        if self.name in self._bucket._store:
            del self._bucket._store[self.name]
        else:
            raise FileNotFoundError(self.name)


class _FakeBucket:
    def __init__(self, name):
        self.name = name
        self._store = {}

    def blob(self, blob_name):
        return _FakeBlob(self, blob_name)


class _FakeClient:
    def __init__(self):
        self._buckets = {}

    def bucket(self, name):
        if name not in self._buckets:
            self._buckets[name] = _FakeBucket(name)
        return self._buckets[name]

    def list_blobs(self, bucket_name, prefix=''):
        bucket = self.bucket(bucket_name)
        for name in sorted(bucket._store.keys()):
            if name.startswith(prefix):
                yield _FakeBlob(bucket, name)


def _install_fake(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(pending, "_get_client", lambda: fake)
    return fake


def test_enqueue_and_count_roundtrip(monkeypatch):
    _install_fake(monkeypatch)

    pending.enqueue('pending-calls', 'acme.com', 'block-1', meta={'id': 'c1'}, unique_id='c1')
    pending.enqueue('pending-calls', 'acme.com', 'block-2', meta={'id': 'c2'}, unique_id='c2')

    assert pending.count('pending-calls', 'acme.com') == 2
    assert pending.count('pending-calls', 'unknown.co') == 0


def test_list_partitions_returns_only_partitions_with_items(monkeypatch):
    _install_fake(monkeypatch)
    pending.enqueue('pending-calls', 'acme.com', 'a', unique_id='a')
    pending.enqueue('pending-calls', 'widgets.io', 'b', unique_id='b')
    pending.enqueue('pending-messages', 'C123', 'm1', unique_id='m1')

    assert pending.list_partitions('pending-calls') == {'acme.com', 'widgets.io'}
    assert pending.list_partitions('pending-messages') == {'C123'}


def test_drain_yields_lexicographic_order_and_payload(monkeypatch):
    fake = _install_fake(monkeypatch)

    # Inject keys with controlled ordering by writing directly.
    bucket = fake.bucket(pending.DEFAULT_BUCKET)
    bucket._store['pending-calls/acme.com/0001-a.json'] = json.dumps({
        'id': 'a', 'content': 'A', 'meta': {},
    })
    bucket._store['pending-calls/acme.com/0002-b.json'] = json.dumps({
        'id': 'b', 'content': 'B', 'meta': {},
    })
    bucket._store['pending-calls/acme.com/0003-c.json'] = json.dumps({
        'id': 'c', 'content': 'C', 'meta': {},
    })

    drained = list(pending.drain('pending-calls', 'acme.com'))

    assert [k for k, _ in drained] == [
        '0001-a.json', '0002-b.json', '0003-c.json',
    ]
    assert [p['content'] for _, p in drained] == ['A', 'B', 'C']


def test_delete_removes_blob_and_is_idempotent(monkeypatch):
    _install_fake(monkeypatch)

    pending.enqueue('pending-calls', 'acme.com', 'X', unique_id='X')
    assert pending.count('pending-calls', 'acme.com') == 1

    keys = [k for k, _ in pending.drain('pending-calls', 'acme.com')]
    assert len(keys) == 1
    pending.delete('pending-calls', 'acme.com', keys[0])
    assert pending.count('pending-calls', 'acme.com') == 0

    # Second delete must not raise.
    pending.delete('pending-calls', 'acme.com', keys[0])
