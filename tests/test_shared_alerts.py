"""Tests for shared/alerts.py - doc_full Cloud Monitoring log line.

Hard contract we lock down:
  * exactly one WARNING-level record per call when not deduped
  * structured `json_fields` shape with EXACT field names (a refactor
    that renames `service` -> `svc` would silently break the prod
    Cloud Monitoring filter `jsonPayload.service="gong"`, so we pin
    the names here)
  * never raises (logger fault, encoding fault, anything)
  * per-run dedup via `alerted_customers`; key is added BEFORE emit
    so a hung logger can't re-enter the same run
"""
import logging

import pytest

import shared.alerts as alerts


@pytest.fixture
def cap(caplog):
    """caplog scoped to shared.alerts at WARNING."""
    caplog.set_level(logging.WARNING, logger="shared.alerts")
    return caplog


def _records(cap):
    return [r for r in cap.records if r.name == "shared.alerts"]


def test_emits_warning_with_structured_fields(cap):
    out = alerts.send_doc_full_alert(
        customer_label="Acme",
        customer_key="acme.com",
        doc_ids=["doc-A", "doc-B"],
        pending_count=7,
        service="gong",
    )
    assert out is True

    records = _records(cap)
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.WARNING
    # Human-readable fallback: works without google-cloud-logging.
    assert rec.getMessage().startswith("doc_full ")
    assert "service=gong" in rec.getMessage()
    assert "customer=Acme" in rec.getMessage()

    # Lock the structured-field NAMES exactly. The prod Cloud Monitoring
    # filter is `jsonPayload.event="doc_full"` and operator alert routing
    # depends on jsonPayload.service / .customer_label being these names.
    assert rec.json_fields == {
        "event": "doc_full",
        "service": "gong",
        "customer_label": "Acme",
        "customer_key": "acme.com",
        "doc_ids": ["doc-A", "doc-B"],
        "pending_count": 7,
    }


def test_dedups_within_run(cap):
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
    assert alerted == {"acme.com"}
    assert len(_records(cap)) == 1


def test_no_dedup_set_emits_every_time(cap):
    alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=1, service="gong",
    )
    alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=2, service="gong",
    )
    assert len(_records(cap)) == 2


def test_distinct_services_distinct_records(cap):
    alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=1, service="gong",
    )
    alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="C0XYZ",
        doc_ids=["doc-A"], pending_count=2, service="slack",
    )
    records = _records(cap)
    assert len(records) == 2
    services = [r.json_fields["service"] for r in records]
    assert services == ["gong", "slack"]


def test_never_raises_on_logging_failure(monkeypatch):
    """A broken logger must not bubble - data is already safe in GCS."""
    def boom(*args, **kwargs):
        raise RuntimeError("logging stack down")
    monkeypatch.setattr(alerts.logger, "warning", boom)

    alerted = set()
    out = alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=1, service="gong",
        alerted_customers=alerted,
    )

    assert out is False
    # Dedup-set was updated BEFORE the (failing) emit attempt: a
    # repeat call in the same run must not retry.
    assert alerted == {"acme.com"}
    out2 = alerts.send_doc_full_alert(
        customer_label="Acme", customer_key="acme.com",
        doc_ids=["doc-A"], pending_count=2, service="gong",
        alerted_customers=alerted,
    )
    assert out2 is False
