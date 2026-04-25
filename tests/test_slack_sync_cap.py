"""Cap-aware behaviour tests for slack-sync/main.py.

These cover the parts of the cap-hit flow that DON'T overlap with
verify_slack_signature (already tested in test_slack_sync_helpers.py):

  * _webhook_dedup_text caches by channel_id, invalidates when the
    doc-id list changes
  * _handle_webhook_message buffers to GCS on DocFullError WITHOUT
    calling shared.alerts (3s budget)
  * backfill_channel switches to buffer-only mode after the first cap
    hit, fires exactly one alert
  * drain_channel stops on first cap hit per channel
  * handle_drain dispatches to drain_channel for every partition with
    a current mapping
  * backfill_channel uses channel.created when oldest_ts is None
"""
from unittest.mock import MagicMock

from shared.google_docs import DocFullError


def _make_request(args):
    req = MagicMock()
    req.args = args
    return req


# -----------------------------------------------------------------------------
# _webhook_dedup_text caching
# -----------------------------------------------------------------------------


def test_webhook_dedup_text_caches_within_session(slack_main, monkeypatch):
    slack_main._webhook_doc_cache.clear()

    calls = []
    def _read(d):
        calls.append(d)
        return f"text-of-{d}"
    monkeypatch.setattr(slack_main, "get_doc_text", _read)

    out1 = slack_main._webhook_dedup_text("C1", ["doc-A"])
    out2 = slack_main._webhook_dedup_text("C1", ["doc-A"])

    assert out1 == out2 == "text-of-doc-A"
    assert calls == ["doc-A"]  # only read once


def test_webhook_dedup_text_invalidates_when_doc_ids_change(slack_main, monkeypatch):
    """Operator extends doc list -> cache invalidates on next webhook."""
    slack_main._webhook_doc_cache.clear()

    calls = []
    def _read(d):
        calls.append(d)
        return f"text-of-{d}"
    monkeypatch.setattr(slack_main, "get_doc_text", _read)

    slack_main._webhook_dedup_text("C1", ["doc-A"])
    slack_main._webhook_dedup_text("C1", ["doc-A", "doc-B"])

    # Both reads happened on the second call: full re-fetch.
    assert calls == ["doc-A", "doc-A", "doc-B"]


# -----------------------------------------------------------------------------
# _handle_webhook_message: buffer-only on DocFullError, no SendGrid
# -----------------------------------------------------------------------------


def test_handle_webhook_message_buffers_without_alert(slack_main, monkeypatch):
    """3-second budget: webhook NEVER calls shared.alerts."""
    slack_main._webhook_doc_cache.clear()
    monkeypatch.setattr(slack_main, "get_doc_text", lambda d: "")
    monkeypatch.setattr(slack_main, "get_user_name", lambda u: "alice")

    def _full(*a, **kw):
        raise DocFullError("doc-A", 7_000_000)
    monkeypatch.setattr(slack_main, "append_to_doc", _full)

    enqueue_mock = MagicMock()
    monkeypatch.setattr(slack_main.pending, "enqueue", enqueue_mock)

    alert_mock = MagicMock()
    monkeypatch.setattr(slack_main.alerts, "send_doc_full_alert", alert_mock)

    event = {
        "type": "message",
        "channel": "C1",
        "user": "U1",
        "ts": "1700000000.000100",
        "text": "hello",
    }
    mapping = {"docId": "doc-A", "customerName": "Acme"}

    slack_main._handle_webhook_message(event, mapping)

    enqueue_mock.assert_called_once()
    enqueue_args = enqueue_mock.call_args
    assert enqueue_args.args[0] == slack_main.pending.PREFIX_SLACK
    assert enqueue_args.args[1] == "C1"
    assert enqueue_args.kwargs.get("unique_id") == "1700000000.000100"
    alert_mock.assert_not_called()


def test_handle_webhook_message_dedups_against_concatenation(slack_main, monkeypatch):
    """A message already in doc-A must NOT be appended to doc-B."""
    slack_main._webhook_doc_cache.clear()

    by_doc = {
        "doc-A": "[03/15/2024, 09:00 AM] alice:\nhello\n\n",
        "doc-B": "",
    }
    monkeypatch.setattr(slack_main, "get_doc_text", lambda d: by_doc[d])
    monkeypatch.setattr(slack_main, "get_user_name", lambda u: "alice")
    monkeypatch.setattr(slack_main, "format_timestamp", lambda ts: "03/15/2024, 09:00 AM")

    append_mock = MagicMock()
    monkeypatch.setattr(slack_main, "append_to_doc", append_mock)

    event = {"type": "message", "channel": "C1", "user": "U1", "ts": "1", "text": "hello"}
    mapping = {"docId": "doc-A,doc-B", "customerName": "Acme"}

    slack_main._handle_webhook_message(event, mapping)

    append_mock.assert_not_called()


# -----------------------------------------------------------------------------
# backfill_channel cap-hit behaviour
# -----------------------------------------------------------------------------


def _make_messages(n):
    return [
        {"user": f"U{i}", "ts": f"{1700000000 + i}.000000", "text": f"msg-{i}"}
        for i in range(n)
    ]


def test_backfill_channel_buffers_remainder_after_first_cap_hit(slack_main, monkeypatch):
    """After the first DocFullError, every subsequent message goes
    straight to the queue with NO retry against the doc."""
    fake_slack = MagicMock()
    fake_slack.conversations_history.return_value = {
        'messages': _make_messages(3),
        'response_metadata': {'next_cursor': ''},
    }
    monkeypatch.setattr(slack_main, "get_slack_client", lambda: fake_slack)
    monkeypatch.setattr(slack_main, "get_doc_text", lambda d: "")
    monkeypatch.setattr(slack_main, "get_user_name", lambda u: u)

    append_calls = []
    def _maybe_full(doc_ids, content, current_text_bytes=None):
        append_calls.append(content)
        if len(append_calls) == 1:
            return doc_ids[-1]
        raise DocFullError("doc-A", 7_000_000)
    monkeypatch.setattr(slack_main, "append_to_doc", _maybe_full)

    enqueue_mock = MagicMock()
    monkeypatch.setattr(slack_main.pending, "enqueue", enqueue_mock)
    monkeypatch.setattr(slack_main.pending, "count", lambda *a, **kw: 2)

    alert_mock = MagicMock(return_value=True)
    monkeypatch.setattr(slack_main.alerts, "send_doc_full_alert", alert_mock)

    out = slack_main.backfill_channel("C1", {"docId": "doc-A", "customerName": "Acme"}, oldest_ts=0)

    assert out["added"] == 1
    assert out["buffered"] == 2
    # First DocFullError counts as one append attempt; we don't retry
    # message 3 against the doc.
    assert len(append_calls) == 2
    assert enqueue_mock.call_count == 2
    alert_mock.assert_called_once()


def test_backfill_channel_defaults_oldest_to_channel_created(slack_main, monkeypatch):
    """No oldest_ts arg -> conversations.info gives us channel.created."""
    fake_slack = MagicMock()
    fake_slack.conversations_history.return_value = {'messages': [], 'response_metadata': {}}
    fake_slack.conversations_info.return_value = {'channel': {'created': 1234567890}}
    monkeypatch.setattr(slack_main, "get_slack_client", lambda: fake_slack)
    monkeypatch.setattr(slack_main, "get_doc_text", lambda d: "")

    slack_main.backfill_channel("C1", {"docId": "doc-A", "customerName": "Acme"})

    fake_slack.conversations_history.assert_called_once()
    kwargs = fake_slack.conversations_history.call_args.kwargs
    assert kwargs['oldest'] == '1234567890'


# -----------------------------------------------------------------------------
# drain_channel + handle_drain
# -----------------------------------------------------------------------------


def test_drain_channel_appends_then_deletes(slack_main, monkeypatch):
    monkeypatch.setattr(slack_main, "get_doc_text", lambda d: "")
    drain_items = [
        ("k1.json", {"id": "1", "content": "B1", "meta": {}}),
        ("k2.json", {"id": "2", "content": "B2", "meta": {}}),
    ]
    monkeypatch.setattr(slack_main.pending, "drain", lambda p, c: iter(drain_items))
    delete_mock = MagicMock()
    monkeypatch.setattr(slack_main.pending, "delete", delete_mock)
    append_mock = MagicMock(return_value="doc-A")
    monkeypatch.setattr(slack_main, "append_to_doc", append_mock)

    out = slack_main.drain_channel("C1", {"docId": "doc-A", "customerName": "Acme"})

    assert out == {"channel": "C1", "drained": 2}
    assert append_mock.call_count == 2
    assert delete_mock.call_count == 2


def test_drain_channel_stops_on_first_cap_hit(slack_main, monkeypatch):
    monkeypatch.setattr(slack_main, "get_doc_text", lambda d: "")
    drain_items = [
        ("k1.json", {"id": "1", "content": "B1", "meta": {}}),
        ("k2.json", {"id": "2", "content": "B2", "meta": {}}),
    ]
    monkeypatch.setattr(slack_main.pending, "drain", lambda p, c: iter(drain_items))
    monkeypatch.setattr(slack_main.pending, "count", lambda *a, **kw: 5)
    delete_mock = MagicMock()
    monkeypatch.setattr(slack_main.pending, "delete", delete_mock)

    def _full(*a, **kw):
        raise DocFullError("doc-A", 7_000_000)
    monkeypatch.setattr(slack_main, "append_to_doc", _full)

    alert_mock = MagicMock(return_value=True)
    monkeypatch.setattr(slack_main.alerts, "send_doc_full_alert", alert_mock)

    out = slack_main.drain_channel("C1", {"docId": "doc-A", "customerName": "Acme"})

    assert out == {"channel": "C1", "drained": 0}
    delete_mock.assert_not_called()
    alert_mock.assert_called_once()


def test_handle_drain_dispatches_per_partition(slack_main, monkeypatch):
    monkeypatch.setattr(slack_main.pending, "list_partitions", lambda p: {"C1", "C2"})
    monkeypatch.setattr(
        slack_main, "get_channel_mapping",
        lambda: {
            "C1": {"docId": "doc-A", "customerName": "Acme"},
            "C2": {"docId": "doc-B", "customerName": "Widgets"},
        },
    )

    drain_calls = []
    def _drain(channel_id, mapping):
        drain_calls.append(channel_id)
        return {"channel": channel_id, "drained": 1}
    monkeypatch.setattr(slack_main, "drain_channel", _drain)

    body, status = slack_main.handle_drain(MagicMock())

    assert status == 200
    assert body["drained"] == 2
    assert sorted(drain_calls) == ["C1", "C2"]


def test_handle_drain_skips_partitions_without_mapping(slack_main, monkeypatch):
    """Partitions whose channel is no longer mapped are left in GCS so
    the operator notices."""
    monkeypatch.setattr(slack_main.pending, "list_partitions", lambda p: {"C1", "C-orphan"})
    monkeypatch.setattr(slack_main, "get_channel_mapping", lambda: {
        "C1": {"docId": "doc-A", "customerName": "Acme"},
    })

    drain_calls = []
    def _drain(channel_id, mapping):
        drain_calls.append(channel_id)
        return {"channel": channel_id, "drained": 0}
    monkeypatch.setattr(slack_main, "drain_channel", _drain)

    body, status = slack_main.handle_drain(MagicMock())

    assert status == 200
    assert drain_calls == ["C1"]


# -----------------------------------------------------------------------------
# slack_webhook routing of new GET endpoints
# -----------------------------------------------------------------------------


def test_slack_webhook_routes_drain_get(slack_main, monkeypatch):
    monkeypatch.setattr(slack_main, "handle_drain", lambda r: ({"drained": 0}, 200))
    monkeypatch.setattr(slack_main, "handle_backfill", lambda r: pytest_fail("backfill should not be called"))

    req = MagicMock()
    req.method = 'GET'
    req.args = {'drain': 'true'}

    body, status = slack_main.slack_webhook(req)
    assert status == 200
    assert body == {"drained": 0}


def pytest_fail(msg):
    raise AssertionError(msg)
