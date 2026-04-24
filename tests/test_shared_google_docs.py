"""Tests for shared/google_docs.py - the cap-aware doc helper.

Today (after this commit) we just lock in the contract: `DocFullError`
exists, carries the offending doc id and the measured byte count, and
`DOC_CAP_BYTES` is set to 6 MB. The caller-side enforcement (refusing
to append above the cap) lands in commit 7; until then `append_to_doc`
keeps its existing behaviour.
"""
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
