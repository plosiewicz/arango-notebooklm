"""Tests for pure helpers in config-sync/main.py.

Covers the new dispatcher onboarding flow:
  * _dispatch: success, timeout-as-success, connection-error
  * process_slack_tab: dispatches with no `oldest` param so slack-sync
    defaults to channel.created; marks Config done = Y on dispatch
  * process_gong_tab: dispatches `full_backfill=true&account=<domain>`
    and marks Config done = Y on dispatch
  * fire_slack_drain: GET ?drain=true at end of every run
  * config_sync: fires drain after both tabs are processed

`get_column_letter` lives in shared/sheets.py and is tested in
test_shared_sheets.py.
"""
from unittest.mock import MagicMock

import pytest


def test_dispatch_returns_dispatched_on_success(config_main, monkeypatch):
    fake_get = MagicMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr(config_main.http_requests, "get", fake_get)

    status, err = config_main._dispatch("http://x", {"k": "v"}, "label")

    assert (status, err) == ('dispatched', None)
    fake_get.assert_called_once_with(
        "http://x",
        params={"k": "v"},
        timeout=config_main.DISPATCH_TIMEOUT_SECONDS,
    )


def test_dispatch_timeout_is_treated_as_dispatched(config_main, monkeypatch):
    """Fire-and-forget: a request timeout means the sync service is
    still running on the other side, we just don't wait. Caller must
    not treat this as failure."""
    def boom(*args, **kwargs):
        raise config_main.http_requests.Timeout("simulated")
    monkeypatch.setattr(config_main.http_requests, "get", boom)

    assert config_main._dispatch("http://x", {}, "label") == ('dispatched', None)


def test_dispatch_connection_error_returns_error(config_main, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("cant connect")
    monkeypatch.setattr(config_main.http_requests, "get", boom)

    status, err = config_main._dispatch("http://x", {}, "label")
    assert status == 'error'
    assert "cant connect" in err


@pytest.fixture
def _stubbed_sheet(monkeypatch, config_main):
    """Stub out the GCS mapping + sheet writes so tests can focus on dispatch."""
    monkeypatch.setattr(config_main, "load_mapping", lambda blob: {})
    monkeypatch.setattr(config_main, "save_mapping", lambda blob, m: None)
    monkeypatch.setattr(config_main, "write_cell", MagicMock())
    return None


def test_process_slack_tab_dispatches_without_oldest_param(config_main, monkeypatch, _stubbed_sheet):
    """The new contract: omit `oldest` so slack-sync defaults to channel.created."""
    monkeypatch.setattr(
        config_main, "read_tab",
        lambda sid, tab: [
            {
                "Slack Channel ID": "C1",
                "Document ID": "doc-1",
                "Customer Name": "Acme",
                "Config done (Y/N)": "",
                "_row_index": 2,
            },
        ],
    )
    fake_get = MagicMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr(config_main.http_requests, "get", fake_get)

    results = config_main.process_slack_tab()

    fake_get.assert_called_once_with(
        config_main.SLACK_SYNC_URL,
        params={"backfill": "true", "channel": "C1"},
        timeout=config_main.DISPATCH_TIMEOUT_SECONDS,
    )
    assert results == [{
        "channel": "C1",
        "customer": "Acme",
        "status": "dispatched",
        "error": None,
    }]
    config_main.write_cell.assert_called_once()
    args = config_main.write_cell.call_args.args
    assert args[0] == config_main.SHEET_ID
    assert args[1] == config_main.SLACK_TAB
    assert args[3] == 'Y'


def test_process_gong_tab_dispatches_full_backfill(config_main, monkeypatch, _stubbed_sheet):
    """Gong onboarding now triggers full_backfill so the sync runs the
    entire Gong retention window on the gong-sync side, not here."""
    monkeypatch.setattr(
        config_main, "read_tab",
        lambda sid, tab: [
            {
                "customer-email-domain": "acme.com",
                "document-id": "doc-A",
                "customer-name": "Acme",
                "Config done (Y/N)": "",
                "_row_index": 2,
            },
        ],
    )
    fake_get = MagicMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr(config_main.http_requests, "get", fake_get)

    results = config_main.process_gong_tab()

    fake_get.assert_called_once_with(
        config_main.GONG_SYNC_URL,
        params={"full_backfill": "true", "account": "acme.com"},
        timeout=config_main.DISPATCH_TIMEOUT_SECONDS,
    )
    assert results[0]["status"] == "dispatched"
    config_main.write_cell.assert_called_once()


def test_dispatch_does_not_mark_done_on_error(config_main, monkeypatch, _stubbed_sheet):
    monkeypatch.setattr(
        config_main, "read_tab",
        lambda sid, tab: [
            {
                "Slack Channel ID": "C1",
                "Document ID": "doc-1",
                "Customer Name": "Acme",
                "Config done (Y/N)": "",
                "_row_index": 2,
            },
        ],
    )
    def boom(*a, **kw):
        raise RuntimeError("dns fail")
    monkeypatch.setattr(config_main.http_requests, "get", boom)

    results = config_main.process_slack_tab()

    assert results[0]["status"] == "error"
    config_main.write_cell.assert_not_called()


def test_config_sync_fires_slack_drain_at_end(config_main, monkeypatch):
    monkeypatch.setattr(config_main, "process_slack_tab", lambda: [])
    monkeypatch.setattr(config_main, "process_gong_tab", lambda: [])
    fake_get = MagicMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr(config_main.http_requests, "get", fake_get)

    body, status = config_main.config_sync(MagicMock())

    assert status == 200
    assert body["slack_drain"] == "dispatched"
    fake_get.assert_called_once_with(
        config_main.SLACK_SYNC_URL,
        params={"drain": "true"},
        timeout=config_main.DISPATCH_TIMEOUT_SECONDS,
    )
