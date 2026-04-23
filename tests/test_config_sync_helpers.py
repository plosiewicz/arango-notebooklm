"""Tests for pure helpers in config-sync/main.py.

No real HTTP, no real Google Sheets - we cover:
  * parse_date_to_ts: five accepted formats + unparseable input
  * get_column_letter: A, B, Z, unknown -> None
  * determine_slack_backfill_ts: explicit date wins; else channel-created
    capped at Jan 1 2024; else Jan 1 2024
  * determine_gong_backfill_days: explicit date wins; else days-since-
    Jan-1-2024; both clamped to >= 1
"""
from datetime import datetime, timezone

from freezegun import freeze_time


def test_parse_date_to_ts_accepts_each_supported_format(config_main):
    """All five formats resolve the same calendar date to a deterministic ts."""
    cases = [
        "03/25/2024",
        "2024-03-25",
        "03-25-2024",
        "03/25/24",
        "2024/03/25",
    ]
    expected = int(datetime(2024, 3, 25).timestamp())
    for s in cases:
        assert config_main.parse_date_to_ts(s) == expected, f"format failed: {s!r}"


def test_parse_date_to_ts_returns_none_for_empty_and_unparseable(config_main):
    assert config_main.parse_date_to_ts("") is None
    assert config_main.parse_date_to_ts("   ") is None
    assert config_main.parse_date_to_ts(None) is None
    assert config_main.parse_date_to_ts("not a date") is None
    assert config_main.parse_date_to_ts("2024-13-45") is None


def test_get_column_letter_common_positions(config_main):
    headers = ["A-col", "B-col", "C-col"]
    assert config_main.get_column_letter(headers, "A-col") == "A"
    assert config_main.get_column_letter(headers, "B-col") == "B"


def test_get_column_letter_far_column(config_main):
    headers = [f"col{i}" for i in range(26)]  # A..Z
    assert config_main.get_column_letter(headers, "col25") == "Z"


def test_get_column_letter_missing_header_returns_none(config_main):
    assert config_main.get_column_letter(["x", "y"], "not there") is None


def test_determine_slack_backfill_ts_explicit_date_wins(config_main, monkeypatch):
    monkeypatch.setattr(
        config_main, "get_slack_channel_created_ts",
        lambda _c: pytest_fail("should not be called"),
    )
    row = {"Backlog through": "06/15/2023", "Slack Channel ID": "C1"}
    assert config_main.determine_slack_backfill_ts(row) == int(datetime(2023, 6, 15).timestamp())


def test_determine_slack_backfill_ts_falls_back_to_channel_created_capped(config_main, monkeypatch):
    """Channel-created used only if it's earlier than Jan 1 2024; else cap wins."""
    created_before = int(datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(config_main, "get_slack_channel_created_ts", lambda _c: created_before)
    row = {"Backlog through": "", "Slack Channel ID": "C1"}
    assert config_main.determine_slack_backfill_ts(row) == created_before

    created_after = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(config_main, "get_slack_channel_created_ts", lambda _c: created_after)
    assert config_main.determine_slack_backfill_ts(row) == config_main.JAN_1_2024_TS


def test_determine_slack_backfill_ts_default_to_jan_1_2024(config_main, monkeypatch):
    monkeypatch.setattr(config_main, "get_slack_channel_created_ts", lambda _c: None)
    row = {"Backlog through": "", "Slack Channel ID": ""}
    assert config_main.determine_slack_backfill_ts(row) == config_main.JAN_1_2024_TS


@freeze_time("2026-03-25 00:00:00", tz_offset=0)
def test_determine_gong_backfill_days_explicit_date(config_main):
    # 10 days before the frozen "now" - tz-aware delta
    row = {"backlog-through": "03/15/2026"}
    assert config_main.determine_gong_backfill_days(row) == 10


@freeze_time("2026-03-25 00:00:00", tz_offset=0)
def test_determine_gong_backfill_days_default_since_jan_1_2024(config_main):
    row = {"backlog-through": ""}
    # 2024-01-01 -> 2026-03-25 = 366 (2024) + 365 (2025) + 83 (Jan 31 + Feb 28 + Mar 24) = 814
    assert config_main.determine_gong_backfill_days(row) == 814


@freeze_time("2024-01-02 00:00:00", tz_offset=0)
def test_determine_gong_backfill_days_clamps_to_one(config_main):
    """A backlog date in the future or equal to today must not return 0 or negative."""
    row = {"backlog-through": "01/02/2024"}
    assert config_main.determine_gong_backfill_days(row) == 1


def pytest_fail(msg):
    """Module-level helper: raising inside a lambda keeps the test readable."""
    raise AssertionError(msg)
