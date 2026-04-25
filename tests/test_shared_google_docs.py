"""Tests for shared/google_docs.py - the cap-aware doc helper.

Locks in:
  * `DocFullError` carries the offending doc id and measured bytes
  * `DOC_CAP_BYTES` is 6 MB (tripwire on accidental bumps)
  * `append_to_doc` accepts both a string and a list[str] for the
    doc id arg, and writes to the LAST id in the list (multi-doc
    cap-hit: new content goes on the newest doc)
  * `append_to_doc` returns the doc id it wrote to (drives the
    caller's dedup cache key)

Cap enforcement against `current_text_bytes` lands in commit 7; this
file gets the corresponding tests then.
"""
from unittest.mock import MagicMock

import pytest

import shared.google_docs as gdocs


def test_doc_full_error_carries_context():
    err = gdocs.DocFullError("doc-A", 7_500_000)
    assert err.doc_id == "doc-A"
    assert err.current_bytes == 7_500_000
    assert "doc-A" in str(err)
    assert "7500000" in str(err)


def test_doc_cap_is_6mb():
    """Locked at 6 MB plaintext. Bumping this number is a deliberate
    decision that affects how close we ride to Google's 10 MB hard wall;
    keep this assertion as a tripwire."""
    assert gdocs.DOC_CAP_BYTES == 6 * 1024 * 1024


def _fake_docs_client(end_index=12):
    """googleapiclient client mock shaped just enough for append_to_doc."""
    client = MagicMock()
    client.documents.return_value.get.return_value.execute.return_value = {
        'body': {'content': [{'endIndex': end_index}]},
    }
    client.documents.return_value.batchUpdate.return_value.execute.return_value = {}
    return client


def test_append_to_doc_accepts_string_id(monkeypatch):
    client = _fake_docs_client()
    monkeypatch.setattr(gdocs, "get_docs_client", lambda: client)

    out = gdocs.append_to_doc("doc-A", "hello")

    assert out == "doc-A"
    client.documents.return_value.get.assert_called_once_with(documentId="doc-A")
    client.documents.return_value.batchUpdate.assert_called_once()
    body = client.documents.return_value.batchUpdate.call_args.kwargs['body']
    assert body['requests'][0]['insertText']['text'] == "hello"


def test_append_to_doc_writes_to_last_id_in_list(monkeypatch):
    """Multi-doc contract: new content lands on the NEWEST doc."""
    client = _fake_docs_client()
    monkeypatch.setattr(gdocs, "get_docs_client", lambda: client)

    out = gdocs.append_to_doc(["doc-A", "doc-B", "doc-C"], "hello")

    assert out == "doc-C"
    client.documents.return_value.get.assert_called_once_with(documentId="doc-C")
    client.documents.return_value.batchUpdate.assert_called_once()
    assert client.documents.return_value.batchUpdate.call_args.kwargs['documentId'] == "doc-C"


def test_append_to_doc_empty_list_raises(monkeypatch):
    monkeypatch.setattr(gdocs, "get_docs_client", lambda: MagicMock())
    with pytest.raises(ValueError):
        gdocs.append_to_doc([], "hello")


def test_append_to_doc_raises_doc_full_when_projected_exceeds_cap(monkeypatch):
    """current_bytes + len(content) > cap -> DocFullError BEFORE any API call.

    Pass `current_text_bytes` exactly at the cap and a one-byte append
    must refuse: we want headroom, not strict equality.
    """
    client = _fake_docs_client()
    monkeypatch.setattr(gdocs, "get_docs_client", lambda: client)

    with pytest.raises(gdocs.DocFullError) as exc_info:
        gdocs.append_to_doc("doc-A", "x", current_text_bytes=gdocs.DOC_CAP_BYTES)

    assert exc_info.value.doc_id == "doc-A"
    assert exc_info.value.current_bytes == gdocs.DOC_CAP_BYTES
    client.documents.return_value.get.assert_not_called()
    client.documents.return_value.batchUpdate.assert_not_called()


def test_append_to_doc_allows_append_when_under_cap(monkeypatch):
    """A 5.99 MB append on a 0-byte doc must succeed - the test is
    'projected total > cap', not 'content alone > cap'."""
    client = _fake_docs_client()
    monkeypatch.setattr(gdocs, "get_docs_client", lambda: client)

    big_content = "x" * (gdocs.DOC_CAP_BYTES - 1)
    out = gdocs.append_to_doc("doc-A", big_content, current_text_bytes=0)

    assert out == "doc-A"
    client.documents.return_value.batchUpdate.assert_called_once()


def test_append_to_doc_no_cap_check_when_current_bytes_none(monkeypatch):
    """current_text_bytes=None -> cap is not enforced; any append goes
    through. Used by one-shot scripts that don't have a cached count."""
    client = _fake_docs_client()
    monkeypatch.setattr(gdocs, "get_docs_client", lambda: client)

    out = gdocs.append_to_doc("doc-A", "hello", current_text_bytes=None)
    assert out == "doc-A"
    client.documents.return_value.batchUpdate.assert_called_once()
