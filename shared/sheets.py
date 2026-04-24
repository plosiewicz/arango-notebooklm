"""Google Sheets helpers shared across sync services.

The onboarding sheet is the system of record for which slack channel /
gong account maps to which Google Doc. config-sync reads it on every
run; gong-sync writes back to it (currently just the 'calls-scraped'
column). Centralizing the thin wrapper here avoids two services
growing their own divergent copies.

All functions take `sheet_id` explicitly rather than reading a module
global, so callers keep their own SHEET_ID constant.
"""
from googleapiclient.discovery import build

_sheets_client = None


def get_sheets_client():
    """Return a cached `sheets` v4 Discovery client.

    Lazily built on first call so tests can patch before the first
    real use. Reset the module global (`_sheets_client = None`) in
    per-test fixtures if you need a fresh client between tests.
    """
    global _sheets_client
    if _sheets_client is None:
        _sheets_client = build('sheets', 'v4')
    return _sheets_client


def read_tab(sheet_id, tab_name):
    """Read all rows from `tab_name` as list[dict] keyed by header.

    Short rows are padded with empty strings to the header length, and
    each dict gets a synthetic `_row_index` pinned to the 1-indexed
    sheet row number so callers can write back to the same row.
    Returns [] if the tab has fewer than 2 rows (i.e. no data rows).
    """
    sheets = get_sheets_client()
    result = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f'{tab_name}!A:Z',
    ).execute()

    values = result.get('values', [])
    if len(values) < 2:
        return []

    headers = values[0]
    rows = []
    for i, row in enumerate(values[1:], start=2):
        padded = row + [''] * (len(headers) - len(row))
        row_dict = {headers[j]: padded[j] for j in range(len(headers))}
        row_dict['_row_index'] = i
        rows.append(row_dict)

    return rows


def write_cell(sheet_id, tab_name, cell_ref, value):
    """Write a single value to `tab_name!cell_ref` with RAW input option."""
    sheets = get_sheets_client()
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f'{tab_name}!{cell_ref}',
        valueInputOption='RAW',
        body={'values': [[value]]},
    ).execute()


def get_column_letter(headers, column_name):
    """Return the A-Z column letter for `column_name`, or None if missing.

    Only supports columns A..Z (idx < 26). Adequate for today's tabs
    (~6 columns); extend to AA..ZZ if any tab grows beyond 26 columns.
    """
    try:
        idx = headers.index(column_name)
        return chr(ord('A') + idx)
    except ValueError:
        return None


def parse_id_list(cell_value):
    """Split a comma-separated doc-id cell into a list of ids.

    Onboarding cells now hold either a single id (`doc-abc`) or a
    comma-separated list (`doc-abc,doc-def`) so a customer can grow
    their doc list when one hits the cap. Whitespace is stripped and
    empty fragments are dropped, so trailing commas are forgiven.
    Returns [] for None or all-whitespace input.
    """
    if not cell_value:
        return []
    return [p.strip() for p in cell_value.split(',') if p.strip()]


def batch_update_values(sheet_id, updates):
    """Apply a batch of single-cell updates in one API call.

    `updates` is a list of (range_str, value) pairs, where `range_str`
    includes the tab name, e.g. `("gong!E2", 5)`. Empty list is a
    silent no-op (no API call) so callers can unconditionally invoke
    this at the end of a loop.
    """
    if not updates:
        return

    sheets = get_sheets_client()
    data = [
        {'range': range_str, 'values': [[value]]}
        for range_str, value in updates
    ]
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            'valueInputOption': 'RAW',
            'data': data,
        },
    ).execute()
