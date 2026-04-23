"""Config-sync Cloud Function.

Runs hourly. For each row in the customer onboarding Google Sheet:
  1. Push channel/account -> doc mappings into the GCS config bucket
     so slack-sync and gong-sync pick them up on their next cache miss.
  2. For rows not yet flagged done, trigger a backfill on the relevant
     sync service and mark the row done.
"""
import json
import os
from datetime import datetime, timezone

import requests as http_requests

from shared.gcs_mapping import load_mapping, save_mapping
from shared.secrets import get_secret
from shared.sheets import get_column_letter, read_tab, write_cell

SHEET_ID = '1p8CZ5RBGkFSf6aPnUIz8DXai9_UgNZhj7g1JtbPMvzI'
SLACK_TAB = 'slack'
GONG_TAB = 'gong'

SLACK_SYNC_URL = 'https://us-central1-slack-notebooklm-sync.cloudfunctions.net/slack-sync'
GONG_SYNC_URL = 'https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync'

# Jan 1, 2024 00:00:00 UTC as Unix timestamp
JAN_1_2024_TS = 1704067200


def get_slack_channel_created_ts(channel_id):
    """Return the Slack channel's `created` unix ts via conversations.info, or None."""
    try:
        token = get_secret('slack-bot-token')
    except Exception as e:
        print(f"Could not fetch slack-bot-token from Secret Manager: {e}")
        return None

    try:
        resp = http_requests.get(
            'https://slack.com/api/conversations.info',
            headers={'Authorization': f'Bearer {token}'},
            params={'channel': channel_id},
            timeout=10,
        )
        data = resp.json()
        if data.get('ok'):
            created = data.get('channel', {}).get('created')
            if created:
                return int(created)
    except Exception as e:
        print(f"Error getting channel info for {channel_id}: {e}")

    return None


def parse_date_to_ts(date_str):
    """Parse a date string (various formats) into a Unix timestamp."""
    if not date_str or not date_str.strip():
        return None

    date_str = date_str.strip()

    # Try common date formats
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m-%d-%Y', '%m/%d/%y', '%Y/%m/%d'):
        try:
            dt = datetime.strptime(date_str, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue

    print(f"Could not parse date: {date_str}")
    return None


def determine_slack_backfill_ts(row):
    """Determine the oldest timestamp for Slack backfill.

    Uses 'Backlog through' column if set, otherwise the earlier of
    channel creation date or Jan 1 2024.
    """
    # Check if there's an explicit backfill date
    backlog_date = row.get('Backlog through', '').strip()
    if backlog_date:
        ts = parse_date_to_ts(backlog_date)
        if ts:
            return ts

    # Fall back to channel creation date vs Jan 1 2024 (whichever is earlier)
    channel_id = row.get('Slack Channel ID', '').strip()
    if channel_id:
        created_ts = get_slack_channel_created_ts(channel_id)
        if created_ts:
            return min(created_ts, JAN_1_2024_TS)

    return JAN_1_2024_TS


def determine_gong_backfill_days(row):
    """Determine the number of days to backfill for Gong.

    Uses 'backlog-through' column if set, otherwise days since Jan 1 2024.
    """
    backlog_date = row.get('backlog-through', '').strip()
    if backlog_date:
        ts = parse_date_to_ts(backlog_date)
        if ts:
            days = (datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)).days
            return max(days, 1)

    # Default: days since Jan 1 2024
    days = (datetime.now(timezone.utc) - datetime(2024, 1, 1, tzinfo=timezone.utc)).days
    return max(days, 1)


def process_slack_tab():
    """Process the Slack tab: update channel mapping and trigger backfill for new channels."""
    print("Processing Slack tab...")

    rows = read_tab(SHEET_ID, SLACK_TAB)
    if not rows:
        print("No rows in Slack tab")
        return []

    headers = list(rows[0].keys())
    # Remove our internal _row_index from headers
    headers = [h for h in headers if h != '_row_index']

    # Load current mapping from GCS
    current_mapping = load_mapping('channel-mapping.json')

    # Build new mapping and find new entries
    new_mapping = {}
    new_channels = []

    for row in rows:
        channel_id = row.get('Slack Channel ID', '').strip()
        doc_id = row.get('Document ID', '').strip()
        customer_name = row.get('Customer Name', '').strip()
        config_done = row.get('Config done (Y/N)', '').strip().upper()

        # Skip rows missing required fields
        if not channel_id or not doc_id:
            continue

        # Add to mapping (both done and new rows)
        if config_done == 'Y' or not config_done:
            new_mapping[channel_id] = {
                'docId': doc_id,
                'customerName': customer_name,
            }

        # Track new channels (config not yet done)
        if not config_done and channel_id not in current_mapping:
            new_channels.append(row)

    # Upload updated mapping if it changed
    if new_mapping != current_mapping:
        save_mapping('channel-mapping.json', new_mapping)
        print(f"Updated channel mapping: {len(current_mapping)} -> {len(new_mapping)} channels")
    else:
        print("Channel mapping unchanged")

    # Trigger backfill and mark done for new channels
    results = []
    config_done_col = get_column_letter(headers, 'Config done (Y/N)')

    for row in new_channels:
        channel_id = row.get('Slack Channel ID', '').strip()
        customer_name = row.get('Customer Name', '').strip()
        row_index = row['_row_index']

        print(f"New Slack channel: {customer_name} ({channel_id})")

        # Determine backfill start
        oldest_ts = determine_slack_backfill_ts(row)
        print(f"Backfill from timestamp {oldest_ts} ({datetime.fromtimestamp(oldest_ts, tz=timezone.utc).isoformat()})")

        resp = None
        try:
            resp = http_requests.get(
                SLACK_SYNC_URL,
                params={
                    'backfill': 'true',
                    'channel': channel_id,
                    'oldest': str(oldest_ts),
                },
                timeout=540,
            )
            print(f"Backfill response for {channel_id}: {resp.status_code} {resp.text[:500]}")
            results.append({
                'channel': channel_id,
                'customer': customer_name,
                'status': 'ok' if resp.status_code == 200 else 'error',
                'response': resp.text[:500],
            })
        except Exception as e:
            print(f"Error triggering backfill for {channel_id}: {e}")
            results.append({
                'channel': channel_id,
                'customer': customer_name,
                'status': 'error',
                'error': str(e),
            })

        # Mark Config done = Y only if backfill succeeded; failed rows
        # stay blank so the operator can see and remediate.
        if config_done_col and resp is not None and resp.status_code == 200:
            cell_ref = f'{config_done_col}{row_index}'
            try:
                write_cell(SHEET_ID, SLACK_TAB, cell_ref, 'Y')
                print(f"Marked {cell_ref} as Y")
            except Exception as e:
                print(f"Error marking config done for row {row_index}: {e}")

    return results


def process_gong_tab():
    """Process the Gong tab: update account mapping and trigger backfill for new accounts."""
    print("Processing Gong tab...")

    rows = read_tab(SHEET_ID, GONG_TAB)
    if not rows:
        print("No rows in Gong tab")
        return []

    headers = list(rows[0].keys())
    headers = [h for h in headers if h != '_row_index']

    # Load current mapping from GCS
    current_mapping = load_mapping('account-mapping.json')

    # Build new mapping and find new entries
    new_mapping = {}
    new_accounts = []

    for row in rows:
        email_domain = row.get('customer-email-domain', '').strip()
        doc_id = row.get('document-id', '').strip()
        customer_name = row.get('customer-name', '').strip()
        config_done = row.get('Config done (Y/N)', '').strip().upper()

        # Skip rows missing required fields
        if not email_domain or not doc_id:
            continue

        # Add to mapping (both done and new rows)
        if config_done == 'Y' or not config_done:
            new_mapping[email_domain] = {
                'docId': doc_id,
                'customerName': customer_name,
            }

        # Track new accounts (config not yet done)
        if not config_done and email_domain not in current_mapping:
            new_accounts.append(row)

    # Upload updated mapping if it changed
    if new_mapping != current_mapping:
        save_mapping('account-mapping.json', new_mapping)
        print(f"Updated account mapping: {len(current_mapping)} -> {len(new_mapping)} accounts")
    else:
        print("Account mapping unchanged")

    # Trigger backfill and mark done for new accounts
    results = []
    config_done_col = get_column_letter(headers, 'Config done (Y/N)')

    for row in new_accounts:
        email_domain = row.get('customer-email-domain', '').strip()
        customer_name = row.get('customer-name', '').strip()
        row_index = row['_row_index']

        print(f"New Gong account: {customer_name} ({email_domain})")

        # Determine backfill days
        backfill_days = determine_gong_backfill_days(row)
        print(f"Backfill {backfill_days} days")

        resp = None
        try:
            # account filter scopes gong-sync's per-call work to just this
            # customer; without it, gong-sync runs dedup against every
            # mapped doc and config-sync OOMs waiting for the response.
            resp = http_requests.get(
                GONG_SYNC_URL,
                params={
                    'backfill': 'true',
                    'days': str(backfill_days),
                    'account': email_domain,
                },
                timeout=540,
            )
            print(f"Backfill response for {email_domain}: {resp.status_code} {resp.text[:500]}")
            results.append({
                'account': email_domain,
                'customer': customer_name,
                'status': 'ok' if resp.status_code == 200 else 'error',
                'response': resp.text[:500],
            })
        except Exception as e:
            print(f"Error triggering backfill for {email_domain}: {e}")
            results.append({
                'account': email_domain,
                'customer': customer_name,
                'status': 'error',
                'error': str(e),
            })

        # Mark Config done = Y only if backfill succeeded; failed rows
        # stay blank so the operator can see and remediate.
        if config_done_col and resp is not None and resp.status_code == 200:
            cell_ref = f'{config_done_col}{row_index}'
            try:
                write_cell(SHEET_ID, GONG_TAB, cell_ref, 'Y')
                print(f"Marked {cell_ref} as Y")
            except Exception as e:
                print(f"Error marking config done for row {row_index}: {e}")

    return results


def config_sync(request):
    """Main Cloud Function entry point. Triggered daily by Cloud Scheduler."""
    print(f"Config sync started at {datetime.now(timezone.utc).isoformat()}")

    slack_results = process_slack_tab()
    gong_results = process_gong_tab()

    result = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'slack': {
            'new_channels': len(slack_results),
            'details': slack_results,
        },
        'gong': {
            'new_accounts': len(gong_results),
            'details': gong_results,
        },
    }

    print(f"Config sync complete: {json.dumps(result)}")
    return result, 200


# For local testing
if __name__ == '__main__':
    from flask import Flask, request
    app = Flask(__name__)

    @app.route('/', methods=['GET'])
    def handle():
        return config_sync(request)

    port = int(os.environ.get('PORT', 3002))
    print(f'Config sync server running on port {port}')
    app.run(host='0.0.0.0', port=port)
