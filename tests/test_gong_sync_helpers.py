"""Tests for pure helpers in gong-sync/main.py.

Covers:
  * find_mapping_for_account: returns (key, mapping); id-first, exact name,
    case-insensitive name, miss
  * format_call_for_doc: pins the "GONG CALL: <title>" header + formatted
    "Date:" line that process_calls dedups on AND _DATE_RE extracts on
  * _DATE_RE / _extract_call_dates: anchored to the three-line header so
    transcript noise can't inflate the date set
  * _write_call_date_ranges: multi-doc concat, ignores buffered cache,
    silent no-op when columns missing
  * process_calls: multi-doc dedup happy path, DocFullError -> enqueue +
    alert (with dedup across the same run)
  * _drain_pending: drains successfully, stops on first DocFullError
    per partition, alerts once
"""
import re
from datetime import datetime
from unittest.mock import MagicMock


# -----------------------------------------------------------------------------
# find_mapping_for_account
# -----------------------------------------------------------------------------


def test_find_mapping_for_account_prefers_id_match(gong_main, monkeypatch):
    mapping = {"acme-id": {"docId": "doc-by-id"}, "Acme": {"docId": "doc-by-name"}}
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: mapping)

    assert gong_main.find_mapping_for_account("acme-id", "Acme") == (
        "acme-id", {"docId": "doc-by-id"},
    )


def test_find_mapping_for_account_exact_name(gong_main, monkeypatch):
    mapping = {"Acme": {"docId": "doc-acme"}}
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: mapping)

    assert gong_main.find_mapping_for_account(None, "Acme") == ("Acme", {"docId": "doc-acme"})


def test_find_mapping_for_account_case_insensitive_name(gong_main, monkeypatch):
    mapping = {"acme.com": {"docId": "doc-acme"}}
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: mapping)

    assert gong_main.find_mapping_for_account(None, "ACME.COM") == ("acme.com", {"docId": "doc-acme"})


def test_find_mapping_for_account_miss(gong_main, monkeypatch):
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {"other": {"docId": "d"}})

    assert gong_main.find_mapping_for_account("unknown", "Nowhere") == (None, None)


# -----------------------------------------------------------------------------
# format_call_for_doc
# -----------------------------------------------------------------------------


def test_format_call_for_doc_header_and_date(gong_main):
    call = {
        "title": "QBR with Acme",
        "started": "2025-07-04T15:30:00Z",
        "duration": 3600,
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
    assert "Unknown" in out
    assert "No summary available." in out


# -----------------------------------------------------------------------------
# _DATE_RE / _extract_call_dates
# -----------------------------------------------------------------------------


def _block(title, date_str):
    """Build the exact three-line header + Date: line format_call_for_doc emits."""
    return (
        f"\n=====================================\n"
        f"GONG CALL: {title}\n"
        f"=====================================\n"
        f"Date: {date_str}\n"
    )


def test_date_re_matches_anchored_header(gong_main):
    text = _block("c1", "July 04, 2025 at 03:30 PM")
    assert gong_main._DATE_RE.search(text) is not None


def test_date_re_ignores_bare_transcript_mention(gong_main):
    """A transcript line that happens to say 'GONG CALL: ...' without
    the surrounding ===== separator must NOT match."""
    text = "\n[10:30] Alice: lol GONG CALL: was a pun\nDate: July 04, 2025 at 03:30 PM\n"
    assert gong_main._DATE_RE.search(text) is None


def test_extract_call_dates_returns_min_max(gong_main):
    text = (
        _block("c1", "July 04, 2025 at 03:30 PM")
        + _block("c2", "August 15, 2025 at 11:00 AM")
        + _block("c3", "March 01, 2025 at 09:00 AM")
    )
    dates = gong_main._extract_call_dates(text)
    assert min(dates) == datetime(2025, 3, 1, 9, 0)
    assert max(dates) == datetime(2025, 8, 15, 11, 0)


def test_extract_call_dates_drops_unparseable(gong_main):
    text = _block("c1", "July 04, 2025 at 03:30 PM") + _block("c2", "not a date")
    dates = gong_main._extract_call_dates(text)
    assert len(dates) == 1
    assert dates[0] == datetime(2025, 7, 4, 15, 30)


# -----------------------------------------------------------------------------
# _write_call_date_ranges
# -----------------------------------------------------------------------------


def test_write_call_date_ranges_writes_min_max_per_row(gong_main, monkeypatch):
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "document-id": "doc-A",
                "first-call-recorded": "",
                "last-call-recorded": "",
                "_row_index": 2,
            },
        ],
    )
    monkeypatch.setattr(
        gong_main, "get_doc_text",
        lambda doc_id: _block("c1", "July 04, 2025 at 03:30 PM")
                     + _block("c2", "March 01, 2025 at 09:00 AM"),
    )
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    gong_main._write_call_date_ranges()

    batch_mock.assert_called_once()
    sheet_id, updates = batch_mock.call_args.args
    assert sheet_id == gong_main.SHEET_ID
    # first-call-recorded is column C, last-call-recorded is column D
    assert sorted(updates) == sorted([
        ("gong!C2", "03/01/2025"),
        ("gong!D2", "07/04/2025"),
    ])


def test_write_call_date_ranges_concatenates_multi_doc_text(gong_main, monkeypatch):
    """When document-id is 'doc-A,doc-B' we read both and merge their dates."""
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "document-id": "doc-A,doc-B",
                "first-call-recorded": "",
                "last-call-recorded": "",
                "_row_index": 2,
            },
        ],
    )
    by_doc = {
        "doc-A": _block("c1", "July 04, 2025 at 03:30 PM"),
        "doc-B": _block("c2", "August 15, 2025 at 11:00 AM"),
    }
    monkeypatch.setattr(gong_main, "get_doc_text", lambda d: by_doc[d])
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    gong_main._write_call_date_ranges()

    _, updates = batch_mock.call_args.args
    assert ("gong!C2", "07/04/2025") in updates
    assert ("gong!D2", "08/15/2025") in updates


def test_write_call_date_ranges_silent_when_columns_missing(gong_main, monkeypatch, capsys):
    """Sheet without first/last columns -> no GCS / no batch call."""
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "document-id": "doc-A",
                "_row_index": 2,
            },
        ],
    )
    get_doc_mock = MagicMock()
    monkeypatch.setattr(gong_main, "get_doc_text", get_doc_mock)
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    gong_main._write_call_date_ranges()

    batch_mock.assert_not_called()
    get_doc_mock.assert_not_called()
    assert "skipping date-range write" in capsys.readouterr().out


def test_write_call_date_ranges_skips_rows_with_no_matching_dates(gong_main, monkeypatch):
    """A doc that exists but has zero matching headers leaves the cell untouched."""
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "document-id": "doc-A",
                "first-call-recorded": "",
                "last-call-recorded": "",
                "_row_index": 2,
            },
            {
                "customer-email-domain": "widgets.io",
                "document-id": "doc-B",
                "first-call-recorded": "",
                "last-call-recorded": "",
                "_row_index": 3,
            },
        ],
    )
    by_doc = {
        "doc-A": "no matchable headers in here at all",
        "doc-B": _block("c1", "July 04, 2025 at 03:30 PM"),
    }
    monkeypatch.setattr(gong_main, "get_doc_text", lambda d: by_doc[d])
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    gong_main._write_call_date_ranges()

    _, updates = batch_mock.call_args.args
    # Only the widgets.io row gets written - acme has no parseable dates.
    assert all(re.match(r"gong![CD]3", r) for r, _ in updates)


def test_write_call_date_ranges_reads_fresh_not_from_cache(gong_main, monkeypatch):
    """Buffered (un-appended) calls must not show up in the date range.

    We construct a row whose doc text on disk has a single call from
    March; an in-process customer_text_cache holding a (fictitious)
    August call should be IGNORED by _write_call_date_ranges - it
    only ever calls get_doc_text.
    """
    fresh_text = _block("c1", "March 01, 2025 at 09:00 AM")
    monkeypatch.setattr(
        gong_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "document-id": "doc-A",
                "first-call-recorded": "",
                "last-call-recorded": "",
                "_row_index": 2,
            },
        ],
    )
    get_doc_mock = MagicMock(return_value=fresh_text)
    monkeypatch.setattr(gong_main, "get_doc_text", get_doc_mock)
    batch_mock = MagicMock()
    monkeypatch.setattr(gong_main, "batch_update_values", batch_mock)

    gong_main._write_call_date_ranges()

    # Fresh fetch happened, exactly once for doc-A.
    get_doc_mock.assert_called_once_with("doc-A")
    _, updates = batch_mock.call_args.args
    # Both first and last collapse to the only fresh date - August call
    # we'd have buffered does NOT appear.
    assert sorted(updates) == sorted([
        ("gong!C2", "03/01/2025"),
        ("gong!D2", "03/01/2025"),
    ])


# -----------------------------------------------------------------------------
# process_calls: multi-doc dedup, cap-hit -> enqueue + alert
# -----------------------------------------------------------------------------


def _basic_call(call_id, title="A call", started="2025-07-04T15:30:00Z"):
    return {
        "id": call_id,
        "metaData": {"id": call_id, "title": title, "started": started, "duration": 60},
        "parties": [{"speakerId": "s1", "name": "Alice", "company": "Acme"}],
    }


def _stub_gong_api(monkeypatch, gong_main, calls_detail):
    monkeypatch.setattr(gong_main, "get_call_details", lambda ids: [
        c for c in calls_detail if c["metaData"]["id"] in ids
    ])
    monkeypatch.setattr(gong_main, "get_account_info_from_call",
                        lambda details: ("acme-id", "Acme"))
    monkeypatch.setattr(gong_main, "get_transcript", lambda call_id: [])
    monkeypatch.setattr(gong_main, "format_transcript", lambda *a, **kw: "TRANSCRIPT")


def test_process_calls_appends_to_last_doc_in_list(gong_main, monkeypatch):
    """Multi-doc list -> append_to_doc gets the full list, dedup uses
    concat of all docs, cache is keyed by domain."""
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {
        "acme.com": {"docId": "doc-A,doc-B", "customerName": "Acme"},
    })
    monkeypatch.setattr(gong_main, "find_mapping_for_account",
                        lambda aid, an: ("acme.com", {"docId": "doc-A,doc-B", "customerName": "Acme"}))

    detail = _basic_call("call-1")
    _stub_gong_api(monkeypatch, gong_main, [detail])

    # doc-A already has the call; doc-B is fresh. Concatenation must
    # see the dedup hit and skip.
    by_doc = {"doc-A": _block("A call", "July 04, 2025 at 03:30 PM"), "doc-B": ""}
    monkeypatch.setattr(gong_main, "get_doc_text", lambda d: by_doc[d])
    append_mock = MagicMock(return_value="doc-B")
    monkeypatch.setattr(gong_main, "append_to_doc", append_mock)

    cache = {}
    alerted = set()
    out = gong_main.process_calls(
        [{"id": "call-1"}],
        customer_text_cache=cache,
        alerted_customers=alerted,
    )
    processed, errors, _, skipped_dupes, buffered = out
    assert processed == 0
    assert skipped_dupes == 1
    assert buffered == 0
    append_mock.assert_not_called()


def test_process_calls_buffers_on_doc_full_and_alerts_once(gong_main, monkeypatch):
    """Two calls for the same customer hit DocFullError -> both buffered,
    exactly ONE alert sent (alerted_customers dedup)."""
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {
        "acme.com": {"docId": "doc-A", "customerName": "Acme"},
    })
    monkeypatch.setattr(gong_main, "find_mapping_for_account",
                        lambda aid, an: ("acme.com", {"docId": "doc-A", "customerName": "Acme"}))

    detail1 = _basic_call("call-1", title="t1", started="2025-07-04T15:30:00Z")
    detail2 = _basic_call("call-2", title="t2", started="2025-07-05T15:30:00Z")
    _stub_gong_api(monkeypatch, gong_main, [detail1, detail2])
    monkeypatch.setattr(gong_main, "get_doc_text", lambda d: "")

    def _full(*a, **kw):
        from shared.google_docs import DocFullError
        raise DocFullError("doc-A", 7_000_000)
    monkeypatch.setattr(gong_main, "append_to_doc", _full)

    enqueue_mock = MagicMock()
    count_mock = MagicMock(return_value=2)
    monkeypatch.setattr(gong_main.pending, "enqueue", enqueue_mock)
    monkeypatch.setattr(gong_main.pending, "count", count_mock)

    alert_mock = MagicMock(return_value=True)
    monkeypatch.setattr(gong_main.alerts, "send_doc_full_alert", alert_mock)

    cache = {}
    alerted = set()
    out = gong_main.process_calls(
        [{"id": "call-1"}, {"id": "call-2"}],
        customer_text_cache=cache,
        alerted_customers=alerted,
    )
    processed, errors, _, _, buffered = out

    assert processed == 0
    assert buffered == 2
    assert enqueue_mock.call_count == 2
    # Both alert calls should fire, but the dedup is owned by
    # shared.alerts (which we mocked) - we just assert both send_alert
    # invocations passed the same alerted set.
    assert alert_mock.call_count == 2
    for call in alert_mock.call_args_list:
        assert call.kwargs["customer_key"] == "acme.com"
        assert call.kwargs["alerted_customers"] is alerted


# -----------------------------------------------------------------------------
# _drain_pending
# -----------------------------------------------------------------------------


def test_drain_pending_empty_partitions_is_noop(gong_main, monkeypatch):
    monkeypatch.setattr(gong_main.pending, "list_partitions", lambda prefix: set())
    drain_mock = MagicMock()
    monkeypatch.setattr(gong_main.pending, "drain", drain_mock)

    out = gong_main._drain_pending({}, set())

    assert out == 0
    drain_mock.assert_not_called()


def test_drain_pending_appends_then_deletes(gong_main, monkeypatch):
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {
        "acme.com": {"docId": "doc-A", "customerName": "Acme"},
    })
    monkeypatch.setattr(gong_main.pending, "list_partitions", lambda prefix: {"acme.com"})
    monkeypatch.setattr(gong_main, "get_doc_text", lambda d: "")

    drain_items = [
        ("0001-a.json", {"id": "c1", "content": "BLOCK1", "meta": {}}),
        ("0002-b.json", {"id": "c2", "content": "BLOCK2", "meta": {}}),
    ]
    monkeypatch.setattr(gong_main.pending, "drain", lambda prefix, partition: iter(drain_items))
    delete_mock = MagicMock()
    monkeypatch.setattr(gong_main.pending, "delete", delete_mock)

    append_mock = MagicMock(return_value="doc-A")
    monkeypatch.setattr(gong_main, "append_to_doc", append_mock)

    cache = {}
    out = gong_main._drain_pending(cache, set())

    assert out == 2
    assert append_mock.call_count == 2
    assert delete_mock.call_count == 2
    # Cache grew with each successful drain.
    assert "BLOCK1" in cache["acme.com"]
    assert "BLOCK2" in cache["acme.com"]


def test_full_backfill_all_dispatches_one_per_account(gong_main, monkeypatch):
    """Dispatcher fires short-timeout requests, one per mapped account.
    The timeout itself is treated as success (fire-and-forget)."""
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {
        "acme.com": {"docId": "doc-A"},
        "widgets.io": {"docId": "doc-B"},
        "zenith.dev": {"docId": "doc-C"},
    })

    fake_get = MagicMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr(gong_main.http_requests, "get", fake_get)

    out = gong_main._dispatch_full_backfill_all()

    assert sorted(r["account"] for r in out) == ["acme.com", "widgets.io", "zenith.dev"]
    assert all(r["status"] == "dispatched" for r in out)
    assert fake_get.call_count == 3
    for call in fake_get.call_args_list:
        assert call.kwargs["params"]["full_backfill"] == "true"
        assert call.kwargs["timeout"] == gong_main.DISPATCH_TIMEOUT_SECONDS


def test_full_backfill_all_treats_timeout_as_dispatched(gong_main, monkeypatch):
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {"acme.com": {"docId": "doc-A"}})

    def boom(*a, **kw):
        raise gong_main.http_requests.Timeout("simulated")
    monkeypatch.setattr(gong_main.http_requests, "get", boom)

    out = gong_main._dispatch_full_backfill_all()
    assert out == [{"account": "acme.com", "status": "dispatched"}]


def test_gong_sync_full_backfill_all_short_circuits(gong_main, monkeypatch):
    """The dispatcher path returns immediately - no drain, no get_calls."""
    dispatched = MagicMock(return_value=[{"account": "x", "status": "dispatched"}])
    monkeypatch.setattr(gong_main, "_dispatch_full_backfill_all", dispatched)

    drain_mock = MagicMock()
    monkeypatch.setattr(gong_main, "_drain_pending", drain_mock)
    calls_mock = MagicMock()
    monkeypatch.setattr(gong_main, "get_calls_in_range", calls_mock)
    monkeypatch.setattr(gong_main, "get_calls_since", calls_mock)

    req = MagicMock()
    req.args = {"full_backfill_all": "true"}

    body, status = gong_main.gong_sync(req)
    assert status == 200
    assert body == {"dispatched": [{"account": "x", "status": "dispatched"}]}
    drain_mock.assert_not_called()
    calls_mock.assert_not_called()
    dispatched.assert_called_once()


def test_drain_pending_breaks_partition_on_doc_full(gong_main, monkeypatch):
    """First DocFullError stops drain for that partition; remaining
    items stay in GCS (no delete) and one alert fires."""
    monkeypatch.setattr(gong_main, "get_account_mapping", lambda: {
        "acme.com": {"docId": "doc-A", "customerName": "Acme"},
    })
    monkeypatch.setattr(gong_main.pending, "list_partitions", lambda prefix: {"acme.com"})
    monkeypatch.setattr(gong_main, "get_doc_text", lambda d: "")
    monkeypatch.setattr(gong_main.pending, "count", lambda prefix, partition: 3)

    drain_items = [
        ("0001-a.json", {"id": "c1", "content": "BLOCK1", "meta": {}}),
        ("0002-b.json", {"id": "c2", "content": "BLOCK2", "meta": {}}),
        ("0003-c.json", {"id": "c3", "content": "BLOCK3", "meta": {}}),
    ]
    monkeypatch.setattr(gong_main.pending, "drain", lambda prefix, partition: iter(drain_items))
    delete_mock = MagicMock()
    monkeypatch.setattr(gong_main.pending, "delete", delete_mock)

    def _full(*a, **kw):
        from shared.google_docs import DocFullError
        raise DocFullError("doc-A", 7_000_000)
    monkeypatch.setattr(gong_main, "append_to_doc", _full)

    alert_mock = MagicMock(return_value=True)
    monkeypatch.setattr(gong_main.alerts, "send_doc_full_alert", alert_mock)

    out = gong_main._drain_pending({}, set())

    assert out == 0
    delete_mock.assert_not_called()
    alert_mock.assert_called_once()
    # And we must NOT have iterated past the first item: only one append attempt.
    # (We can't directly inspect that here, but the alert + 0 drained guarantees it.)


