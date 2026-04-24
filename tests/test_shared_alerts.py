"""Tests for shared/alerts.py - SendGrid wrapper.

Hard contract we lock down:
  * never raises (Secret Manager outage, SendGrid 5xx, network error)
  * never sends twice for the same customer in one run when given an
    `alerted_customers` set
  * when ALERT_EMAIL or SENDGRID_FROM is unset, returns False without
    contacting Secret Manager or SendGrid
"""
from unittest.mock import MagicMock

import shared.alerts as alerts


def _enable_env(monkeypatch):
    monkeypatch.setattr(alerts, "ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(alerts, "SENDGRID_FROM", "noreply@example.com")


def test_skips_when_alert_email_unset(monkeypatch):
    monkeypatch.setattr(alerts, "ALERT_EMAIL", "")
    monkeypatch.setattr(alerts, "SENDGRID_FROM", "noreply@example.com")
    # If the function tried to load the secret, the autouse poison fixture would fire.
    assert alerts.send_doc_full_alert(
        customer_label="Acme",
        customer_key="acme.com",
        doc_ids=["doc-A"],
        pending_count=3,
        service="gong",
    ) is False


def test_skips_when_sender_unset(monkeypatch):
    monkeypatch.setattr(alerts, "ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(alerts, "SENDGRID_FROM", "")
    assert alerts.send_doc_full_alert(
        customer_label="Acme",
        customer_key="acme.com",
        doc_ids=["doc-A"],
        pending_count=3,
        service="gong",
    ) is False


def test_dedups_within_run(monkeypatch):
    """Same customer_key in one run = one send attempt, regardless of failures."""
    _enable_env(monkeypatch)
    monkeypatch.setattr(alerts, "get_secret", lambda _name: "fake-key")

    posted = MagicMock(return_value=MagicMock(status_code=202, text=""))
    import requests
    monkeypatch.setattr(requests, "post", posted)

    alerted = set()
    out1 = alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=1, service="gong",
        alerted_customers=alerted,
    )
    out2 = alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=2, service="gong",
        alerted_customers=alerted,
    )

    assert out1 is True
    assert out2 is False
    assert posted.call_count == 1
    assert alerted == {"acme.com"}


def test_secret_manager_failure_returns_false(monkeypatch):
    _enable_env(monkeypatch)
    def boom(_name):
        raise RuntimeError("secret manager down")
    monkeypatch.setattr(alerts, "get_secret", boom)

    out = alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=1, service="gong",
    )
    assert out is False


def test_sendgrid_5xx_returns_false_does_not_raise(monkeypatch):
    _enable_env(monkeypatch)
    monkeypatch.setattr(alerts, "get_secret", lambda _name: "fake-key")

    import requests
    monkeypatch.setattr(
        requests, "post",
        MagicMock(return_value=MagicMock(status_code=503, text="upstream")),
    )
    assert alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=1, service="gong",
    ) is False


def test_sendgrid_network_error_returns_false(monkeypatch):
    _enable_env(monkeypatch)
    monkeypatch.setattr(alerts, "get_secret", lambda _name: "fake-key")

    import requests
    def boom(*a, **kw):
        raise RuntimeError("dns fail")
    monkeypatch.setattr(requests, "post", boom)

    assert alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=1, service="gong",
    ) is False


def test_payload_shape(monkeypatch):
    _enable_env(monkeypatch)
    monkeypatch.setattr(alerts, "get_secret", lambda _name: "fake-key")

    captured = {}
    import requests
    def fake_post(url, headers=None, data=None, timeout=None):
        captured['url'] = url
        captured['headers'] = headers
        import json as _json
        captured['payload'] = _json.loads(data)
        captured['timeout'] = timeout
        return MagicMock(status_code=202, text="")
    monkeypatch.setattr(requests, "post", fake_post)

    alerts.send_doc_full_alert(
        customer_label="Acme",
        customer_key="acme.com",
        doc_ids=["doc-A", "doc-B"],
        pending_count=7,
        service="gong",
    )

    assert captured['url'] == 'https://api.sendgrid.com/v3/mail/send'
    assert captured['headers']['Authorization'] == 'Bearer fake-key'
    p = captured['payload']
    assert p['personalizations'][0]['to'][0]['email'] == 'ops@example.com'
    assert p['from']['email'] == 'noreply@example.com'
    assert 'gong doc full for Acme' in p['subject']
    body = p['content'][0]['value']
    assert 'doc-A, doc-B' in body
    assert 'Pending items in GCS: 7' in body
