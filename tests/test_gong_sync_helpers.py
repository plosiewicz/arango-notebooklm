"""Tests for pure helpers in gong-sync/main.py.

Covers:
  * find_mapping_for_account: id match, exact-name match, case-insensitive
    fallback, miss -> None
  * format_call_for_doc: pins the "GONG CALL: <title>" header + formatted
    "Date:" line that process_calls dedups off of. Changing the format
    without updating dedup is a real bug we want CI to notice.
"""


def test_find_mapping_for_account_prefers_id_match(gong_main, monkeypatch):
    mapping = {"acme-id": {"docId": "doc-by-id"}, "Acme": {"docId": "doc-by-name"}}
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: mapping)

    assert gong_main.find_mapping_for_account("acme-id", "Acme") == {"docId": "doc-by-id"}


def test_find_mapping_for_account_exact_name(gong_main, monkeypatch):
    mapping = {"Acme": {"docId": "doc-acme"}}
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: mapping)

    assert gong_main.find_mapping_for_account(None, "Acme") == {"docId": "doc-acme"}


def test_find_mapping_for_account_case_insensitive_name(gong_main, monkeypatch):
    mapping = {"acme.com": {"docId": "doc-acme"}}
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: mapping)

    assert gong_main.find_mapping_for_account(None, "ACME.COM") == {"docId": "doc-acme"}


def test_find_mapping_for_account_miss(gong_main, monkeypatch):
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {"other": {"docId": "d"}})

    assert gong_main.find_mapping_for_account("unknown", "Nowhere") is None


def test_format_call_for_doc_header_and_date(gong_main):
    """The "GONG CALL: <title>" header and formatted "Date:" line are the
    dedup keys in process_calls. Don't change either without updating
    dedup."""
    call = {
        "title": "QBR with Acme",
        "started": "2025-07-04T15:30:00Z",
        "duration": 3600,  # 1 hour
        "parties": [
            {"name": "Alice", "company": "Acme"},
            {"name": "Bob", "company": ""},
        ],
    }
    out = gong_main.format_call_for_doc(call, transcript="T", summary="S")

    assert "GONG CALL: QBR with Acme" in out
    assert "Date: July 04, 2025 at 03:30 PM" in out
    assert "Duration: 60 minutes" in out
    assert "Alice (Acme)" in out
    assert "Bob" in out
    assert "## AI Summary" in out
    assert "S" in out
    assert "## Full Transcript" in out
    assert "T" in out


def test_format_call_for_doc_handles_missing_fields(gong_main):
    call = {"parties": []}
    out = gong_main.format_call_for_doc(call, transcript="", summary=None)

    assert "Untitled Call" in out
    assert "Duration: 0 minutes" in out
    assert "Unknown" in out  # participants fallback
    assert "No summary available." in out
