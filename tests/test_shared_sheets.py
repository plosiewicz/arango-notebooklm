"""Tests for shared/sheets.py - the thin wrapper around Google Sheets v4.

Covers:
  * read_tab: header-keyed dicts with _row_index, short rows padded,
    tabs with fewer than 2 rows return []
  * get_column_letter: common positions, Z, unknown -> None
  * write_cell: RAW input option, range joins tab + cell
  * batch_update_values: one batchUpdate per call with each cell as a
    ValueRange; empty updates -> no API call
"""
from unittest.mock import MagicMock

import shared.sheets as sheets


def _fake_sheets_client(get_return=None):
    """Build a MagicMock shaped like the googleapiclient Sheets v4 client.

    `get_return` is the dict that `spreadsheets().values().get().execute()`
    should yield; defaults to an empty `{}`.
    """
    client = MagicMock()
    execute_get = client.spreadsheets.return_value.values.return_value.get.return_value.execute
    execute_get.return_value = get_return if get_return is not None else {}
    return client


def test_read_tab_returns_header_keyed_dicts_with_row_index(monkeypatch):
    client = _fake_sheets_client(get_return={
        "values": [
            ["customer-email-domain", "document-id"],
            ["cadence.com", "doc-abc"],
            ["acme.com", "doc-def"],
        ],
    })
    monkeypatch.setattr(sheets, "get_sheets_client", lambda: client)

    rows = sheets.read_tab("SHEET_XYZ", "gong")

    assert rows == [
        {"customer-email-domain": "cadence.com", "document-id": "doc-abc", "_row_index": 2},
        {"customer-email-domain": "acme.com", "document-id": "doc-def", "_row_index": 3},
    ]


def test_read_tab_pads_short_rows_with_empty_strings(monkeypatch):
    """Sheets API omits trailing empty cells. read_tab must pad up to header length."""
    client = _fake_sheets_client(get_return={
        "values": [
            ["a", "b", "c"],
            ["row1-a"],
            ["row2-a", "row2-b"],
        ],
    })
    monkeypatch.setattr(sheets, "get_sheets_client", lambda: client)

    rows = sheets.read_tab("SHEET_XYZ", "t")

    assert rows[0] == {"a": "row1-a", "b": "", "c": "", "_row_index": 2}
    assert rows[1] == {"a": "row2-a", "b": "row2-b", "c": "", "_row_index": 3}


def test_read_tab_empty_or_header_only_returns_empty_list(monkeypatch):
    """Fewer than 2 rows means no data; return []."""
    client = _fake_sheets_client(get_return={"values": [["a", "b"]]})
    monkeypatch.setattr(sheets, "get_sheets_client", lambda: client)
    assert sheets.read_tab("SHEET_XYZ", "t") == []

    client2 = _fake_sheets_client(get_return={"values": []})
    monkeypatch.setattr(sheets, "get_sheets_client", lambda: client2)
    assert sheets.read_tab("SHEET_XYZ", "t") == []


def test_read_tab_requests_correct_range(monkeypatch):
    client = _fake_sheets_client(get_return={"values": []})
    monkeypatch.setattr(sheets, "get_sheets_client", lambda: client)

    sheets.read_tab("SHEET_XYZ", "gong")

    client.spreadsheets.return_value.values.return_value.get.assert_called_once_with(
        spreadsheetId="SHEET_XYZ",
        range="gong!A:Z",
    )


def test_get_column_letter_common_positions():
    headers = ["A-col", "B-col", "C-col"]
    assert sheets.get_column_letter(headers, "A-col") == "A"
    assert sheets.get_column_letter(headers, "B-col") == "B"
    assert sheets.get_column_letter(headers, "C-col") == "C"


def test_get_column_letter_far_column():
    headers = [f"col{i}" for i in range(26)]
    assert sheets.get_column_letter(headers, "col25") == "Z"


def test_get_column_letter_missing_header_returns_none():
    assert sheets.get_column_letter(["x", "y"], "not there") is None


def test_write_cell_issues_values_update_with_raw(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(sheets, "get_sheets_client", lambda: client)

    sheets.write_cell("SHEET_XYZ", "gong", "E5", "Y")

    client.spreadsheets.return_value.values.return_value.update.assert_called_once_with(
        spreadsheetId="SHEET_XYZ",
        range="gong!E5",
        valueInputOption="RAW",
        body={"values": [["Y"]]},
    )
    client.spreadsheets.return_value.values.return_value.update.return_value.execute.assert_called_once()


def test_batch_update_values_issues_one_batchupdate_with_raw(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(sheets, "get_sheets_client", lambda: client)

    sheets.batch_update_values("SHEET_XYZ", [("gong!E2", 5), ("gong!E7", 12)])

    client.spreadsheets.return_value.values.return_value.batchUpdate.assert_called_once_with(
        spreadsheetId="SHEET_XYZ",
        body={
            "valueInputOption": "RAW",
            "data": [
                {"range": "gong!E2", "values": [[5]]},
                {"range": "gong!E7", "values": [[12]]},
            ],
        },
    )
    client.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.assert_called_once()


def test_batch_update_values_empty_is_no_op(monkeypatch):
    """Empty updates must skip the API entirely - autouse poison would fire otherwise."""
    # `get_sheets_client` is already poisoned by the conftest autouse fixture,
    # so if batch_update_values forgets its guard and calls it, this test
    # raises AssertionError from `_fail`. That's exactly the belt-and-suspenders
    # we want.
    sheets.batch_update_values("SHEET_XYZ", [])
    # also assert nothing raised - if we got here, guard works.
