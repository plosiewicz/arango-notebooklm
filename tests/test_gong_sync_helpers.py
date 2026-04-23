"""Tests for pure helpers in gong-sync/main.py.

Covers:
  * find_mapping_for_account: id match, exact-name match, case-insensitive
    fallback, miss -> None
  * format_call_for_doc: pins the "GONG CALL: <title>" header + formatted
    "Date:" line that process_calls dedups off of. Changing the format
    without updating dedup is a real bug we want CI to notice.
  * _write_calls_scraped_column: absolute-count writeback to the gong tab.
    Idempotent, skips cold accounts, silent when column header missing,
    counts the three-line HEADER_PREFIX so transcript noise can't inflate.
"""
from unittest.mock import MagicMock


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


# -----------------------------------------------------------------------------
# _write_calls_scraped_column
# -----------------------------------------------------------------------------


def _header_block(title):
    """Build the exact three-line header format_call_for_doc emits, used
    to synthesize doc-text fixtures that the helper will count."""
    return f"\n=====================================\nGONG CALL: {title}\n====================================="


def test_write_calls_scraped_column_empty_cache_is_noop(gong_main, monkeypatch):
    """No docs touched -> no sheet read, no batch write."""
    read_tab_mock = MagicMock()
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "read_tab", read_tab_mock)
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    gong_main._write_calls_scraped_column({})

    read_tab_mock.assert_not_called()
    batch_mock.assert_not_called()


def test_write_calls_scraped_column_missing_header_logs_and_skips(gong_main, monkeypatch, capsys):
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {"acme.com": {"docId": "doc-A"}})
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {"customer-email-domain": "acme.com", "document-id": "doc-A", "_row_index": 2},
        ],
    )
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    gong_main._write_calls_scraped_column({"doc-A": _header_block("call-1")})

    batch_mock.assert_not_called()
    captured = capsys.readouterr()
    assert "Calls scraped" in captured.out
    assert "skipping write" in captured.out


def test_write_calls_scraped_column_multi_doc_writes_absolute_count(gong_main, monkeypatch):
    """Happy path: two docs with different counts -> one batch call with both."""
    monkeypatch.setattr(
        gong_main, "get_account_mapping",
        lambda: {
            "acme.com": {"docId": "doc-A"},
            "widgets.io": {"docId": "doc-B"},
        },
    )
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "document-id": "doc-A",
                "Calls scraped": "",
                "_row_index": 2,
            },
            {
                "customer-email-domain": "widgets.io",
                "document-id": "doc-B",
                "Calls scraped": "",
                "_row_index": 5,
            },
        ],
    )
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    cache = {
        "doc-A": _header_block("a1") + _header_block("a2") + _header_block("a3"),
        "doc-B": _header_block("b1"),
    }
    gong_main._write_calls_scraped_column(cache)

    batch_mock.assert_called_once()
    sheet_id, updates = batch_mock.call_args.args
    assert sheet_id == gong_main.SHEET_ID
    # Calls scraped is the 3rd column (index 2) -> letter C
    assert sorted(updates) == sorted([("gong!C2", 3), ("gong!C5", 1)])


def test_write_calls_scraped_column_skips_doc_with_no_mapped_row(gong_main, monkeypatch):
    """A touched doc whose docId doesn't match any sheet row is just dropped."""
    monkeypatch.setattr(
        gong_main, "get_account_mapping",
        lambda: {"acme.com": {"docId": "doc-A"}},
    )
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "Calls scraped": "",
                "_row_index": 2,
            },
        ],
    )
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    cache = {
        "doc-A": _header_block("a1"),
        "doc-ORPHAN": _header_block("o1") + _header_block("o2"),  # no row maps here
    }
    gong_main._write_calls_scraped_column(cache)

    batch_mock.assert_called_once()
    _, updates = batch_mock.call_args.args
    assert updates == [("gong!B2", 1)]  # only acme's row, orphan dropped


def test_write_calls_scraped_column_skips_row_not_in_mapping(gong_main, monkeypatch):
    """A sheet row whose customer-email-domain isn't in the mapping is skipped."""
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {"acme.com": {"docId": "doc-A"}})
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "Calls scraped": "",
                "_row_index": 2,
            },
            {
                "customer-email-domain": "unknown.co",
                "Calls scraped": "",
                "_row_index": 3,
            },
            {
                "customer-email-domain": "",  # blank key
                "Calls scraped": "",
                "_row_index": 4,
            },
        ],
    )
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    gong_main._write_calls_scraped_column({"doc-A": _header_block("a1")})

    _, updates = batch_mock.call_args.args
    assert updates == [("gong!B2", 1)]


def test_write_calls_scraped_column_does_not_count_bare_header_in_transcript(gong_main, monkeypatch):
    """A transcript line with the bare string 'GONG CALL: x' must not inflate
    the count - the anchored three-line prefix is what we count."""
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {"acme.com": {"docId": "doc-A"}})
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "Calls scraped": "",
                "_row_index": 2,
            },
        ],
    )
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    # One real header block + a transcript line that happens to contain
    # "GONG CALL: something" on its own line (no preceding ===== separator).
    doc_text = _header_block("real-call") + "\n[00:00] Alice: hey, GONG CALL: was a pun\n"
    gong_main._write_calls_scraped_column({"doc-A": doc_text})

    _, updates = batch_mock.call_args.args
    assert updates == [("gong!B2", 1)]  # count stays at 1, transcript noise ignored
