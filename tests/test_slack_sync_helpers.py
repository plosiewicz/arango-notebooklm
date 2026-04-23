"""Tests for security-critical helpers in slack-sync/main.py.

verify_slack_signature is the one test in the suite we care about
hardest. Any path that should reject a request MUST return False and
MUST NOT raise. The happy path must return True only under a valid HMAC
with a fresh timestamp.

format_timestamp is pinned to a specific string format because it's part
of the doc-dedup key. Test timezone is forced to UTC (see CI) so the
format is deterministic on laptops and the runner alike.
"""
import hashlib
import hmac
import os
from datetime import datetime, timezone

from freezegun import freeze_time


SIGNING_SECRET = "test-signing-secret"
NOW_EPOCH = 1_700_000_000  # arbitrary but fixed: 2023-11-14 22:13:20 UTC
FROZEN_NOW = datetime.fromtimestamp(NOW_EPOCH, tz=timezone.utc)


def _sign(timestamp, body, secret=SIGNING_SECRET):
    basestring = f"v0:{timestamp}:{body}".encode()
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return "v0=" + digest


def _request(headers, body="payload"):
    class R:
        pass

    r = R()
    r.headers = headers
    r.get_data = lambda as_text=True: body
    return r


def test_verify_slack_signature_happy_path(slack_main, monkeypatch):
    monkeypatch.setattr(slack_main, "get_secret", lambda _name: SIGNING_SECRET)

    with freeze_time(FROZEN_NOW):
        body = "payload"
        sig = _sign(NOW_EPOCH, body)
        req = _request({
            "X-Slack-Signature": sig,
            "X-Slack-Request-Timestamp": str(NOW_EPOCH),
        }, body=body)
        assert slack_main.verify_slack_signature(req) is True


def test_verify_slack_signature_rejects_stale_timestamp(slack_main, monkeypatch):
    monkeypatch.setattr(slack_main, "get_secret", lambda _name: SIGNING_SECRET)

    with freeze_time(FROZEN_NOW):
        stale_ts = NOW_EPOCH - 600  # 10 min in the past, outside 5 min window
        body = "payload"
        sig = _sign(stale_ts, body)
        req = _request({
            "X-Slack-Signature": sig,
            "X-Slack-Request-Timestamp": str(stale_ts),
        }, body=body)
        assert slack_main.verify_slack_signature(req) is False


def test_verify_slack_signature_rejects_non_integer_timestamp(slack_main, monkeypatch):
    monkeypatch.setattr(slack_main, "get_secret", lambda _name: SIGNING_SECRET)

    req = _request({
        "X-Slack-Signature": _sign("abc", "body"),
        "X-Slack-Request-Timestamp": "abc",
    }, body="body")
    assert slack_main.verify_slack_signature(req) is False


def test_verify_slack_signature_rejects_missing_headers(slack_main, monkeypatch):
    monkeypatch.setattr(slack_main, "get_secret", lambda _name: SIGNING_SECRET)

    # Missing both
    assert slack_main.verify_slack_signature(_request({})) is False
    # Missing signature
    assert slack_main.verify_slack_signature(
        _request({"X-Slack-Request-Timestamp": str(NOW_EPOCH)})
    ) is False
    # Missing timestamp
    assert slack_main.verify_slack_signature(
        _request({"X-Slack-Signature": "v0=deadbeef"})
    ) is False


def test_verify_slack_signature_rejects_wrong_signature(slack_main, monkeypatch):
    monkeypatch.setattr(slack_main, "get_secret", lambda _name: SIGNING_SECRET)

    with freeze_time(FROZEN_NOW):
        req = _request({
            "X-Slack-Signature": "v0=" + ("0" * 64),
            "X-Slack-Request-Timestamp": str(NOW_EPOCH),
        }, body="payload")
        assert slack_main.verify_slack_signature(req) is False


def test_verify_slack_signature_fails_closed_when_secret_manager_raises(slack_main, monkeypatch):
    """Regression guard for the hardening fix - get_secret raising must NOT
    leak out of verify_slack_signature. See commit:
    fix(slack-sync): verify_slack_signature fails closed on Secret Manager error.
    """
    def boom(_name):
        raise RuntimeError("secret manager down")

    monkeypatch.setattr(slack_main, "get_secret", boom)

    with freeze_time(FROZEN_NOW):
        body = "payload"
        # Even with a well-formed looking signature, we must reject.
        req = _request({
            "X-Slack-Signature": "v0=" + ("a" * 64),
            "X-Slack-Request-Timestamp": str(NOW_EPOCH),
        }, body=body)
        assert slack_main.verify_slack_signature(req) is False


def test_format_message_exact_shape(slack_main):
    """The exact shape is part of our dedup key; don't change without updating
    the "[ts] user:" scanner in backfill_channel."""
    out = slack_main.format_message("alice", "03/25/2024, 09:00 AM", "hello")
    assert out == "[03/25/2024, 09:00 AM] alice:\nhello\n\n"


def test_format_timestamp_exact_shape_utc(slack_main):
    """Requires TZ=UTC (see CI + local setup in CONTRIBUTING) so the 24h -> 12h
    conversion resolves the same wall clock everywhere.
    """
    # 1_700_000_000 -> 2023-11-14 22:13:20 UTC
    assert os.environ.get("TZ") == "UTC", "this test requires TZ=UTC"
    assert slack_main.format_timestamp(1_700_000_000) == "11/14/2023, 10:13 PM"
