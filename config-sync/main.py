"""Config-sync Cloud Function.

Runs hourly. For each row in the customer onboarding Google Sheet:
  1. Push channel/account -> doc mappings into the GCS config bucket
     so slack-sync and gong-sync pick them up on their next cache miss.
  2. For rows not yet flagged done, dispatch a full-history backfill
     to the relevant sync service (fire-and-forget, short timeout) and
     mark the row done. The sync service runs the backfill in its own
     540s budget; config-sync does not wait for completion.
  3. At the end of the run, fire a fire-and-forget request to
     slack-sync's drain endpoint so any messages that were buffered to
     GCS during a doc-cap-hit window get appended to the (newly
     enlarged) doc list.

Backfill scope is decided by the sync services themselves:
  * slack-sync defaults `oldest` to the channel's `created` timestamp
  * gong-sync's full_backfill walks the entire Gong retention window

so config-sync no longer needs date-math or per-row "Backlog through"
columns. The "Backlog through" column on the sheet is now ignored
(see ARCHITECTURE.md for the migration plan).
"""
import json
import os
from datetime import datetime, timezone

import requests as http_requests

from shared.gcs_mapping import load_mapping, save_mapping
from shared.sheets import get_column_letter, read_tab, write_cell

SHEET_ID = '1p8CZ5RBGkFSf6aPnUIz8DXai9_UgNZhj7g1JtbPMvzI'
SLACK_TAB = 'slack'
GONG_TAB = 'gong'

SLACK_SYNC_URL = 'https://us-central1-slack-notebooklm-sync.cloudfunctions.net/slack-sync'
GONG_SYNC_URL = 'https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync'

# Fire-and-forget timeout. Long enough to confirm the sync service
# accepted the dispatch (TLS handshake + initial bytes) but short
# enough that one slow customer can't blow our 540s budget. The sync
# service keeps running after our connection drops.
DISPATCH_TIMEOUT_SECONDS = 5


def _dispatch(url, params, label):
    """Fire a GET at `url` with `params` and a short timeout.

    Returns ('dispatched', None) if the request was accepted (any
    response code, including 200), or ('error', err_str) on connection
    failure. A `requests.Timeout` is treated as success: the sync
    service is still running on the other side, we just don't wait.
    """
    try:
        resp = http_requests.get(url, params=params, timeout=DISPATCH_TIMEOUT_SECONDS)
        print(f"Dispatched {label}: HTTP {resp.status_code}")
        return ('dispatched', None)
    except http_requests.Timeout:
        print(f"Dispatched {label}: timeout (fire-and-forget, sync continues)")
        return ('dispatched', None)
    except Exception as e:
        print(f"Failed to dispatch {label}: {e}")
        return ('error', str(e))


def process_slack_tab():
    """Process the Slack tab: update channel mapping and dispatch backfill for new channels."""
    print("Processing Slack tab...")

    rows = read_tab(SHEET_ID, SLACK_TAB)
    if not rows:
        print("No rows in Slack tab")
        return []

    headers = [h for h in rows[0].keys() if h != '_row_index']

    current_mapping = load_mapping('channel-mapping.json')

    new_mapping = {}
    new_channels = []

    for row in rows:
        channel_id = row.get('Slack Channel ID', '').strip()
        doc_id = row.get('Document ID', '').strip()
        customer_name = row.get('Customer Name', '').strip()
        config_done = row.get('Config done (Y/N)', '').strip().upper()

        if not channel_id or not doc_id:
            continue

        if config_done == 'Y' or not config_done:
            new_mapping[channel_id] = {
                'docId': doc_id,
                'customerName': customer_name,
            }

        if not config_done and channel_id not in current_mapping:
            new_channels.append(row)

    if new_mapping != current_mapping:
        save_mapping('channel-mapping.json', new_mapping)
        print(f"Updated channel mapping: {len(current_mapping)} -> {len(new_mapping)} channels")
    else:
        print("Channel mapping unchanged")

    results = []
    config_done_col = get_column_letter(headers, 'Config done (Y/N)')

    for row in new_channels:
        channel_id = row.get('Slack Channel ID', '').strip()
        customer_name = row.get('Customer Name', '').strip()
        row_index = row['_row_index']

        print(f"New Slack channel: {customer_name} ({channel_id})")

        # No `oldest` param: slack-sync defaults to channel.created so
        # we capture the entire channel history without date-math here.
        status, err = _dispatch(
            SLACK_SYNC_URL,
            {'backfill': 'true', 'channel': channel_id},
            f"slack backfill {channel_id}",
        )
        results.append({
            'channel': channel_id,
            'customer': customer_name,
            'status': status,
            'error': err,
        })

        # Mark done on dispatch (not completion). The sync service is
        # idempotent (content-based dedup), so a repeated dispatch is
        # safe. If dispatch itself fails we leave the cell blank for
        # the operator to remediate.
        if config_done_col and status == 'dispatched':
            cell_ref = f'{config_done_col}{row_index}'
            try:
                write_cell(SHEET_ID, SLACK_TAB, cell_ref, 'Y')
                print(f"Marked {cell_ref} as Y")
            except Exception as e:
                print(f"Error marking config done for row {row_index}: {e}")

    return results


def process_gong_tab():
    """Process the Gong tab: update account mapping and dispatch full backfill for new accounts."""
    print("Processing Gong tab...")

    rows = read_tab(SHEET_ID, GONG_TAB)
    if not rows:
        print("No rows in Gong tab")
        return []

    headers = [h for h in rows[0].keys() if h != '_row_index']

    current_mapping = load_mapping('account-mapping.json')

    new_mapping = {}
    new_accounts = []

    for row in rows:
        email_domain = row.get('customer-email-domain', '').strip()
        doc_id = row.get('document-id', '').strip()
        customer_name = row.get('customer-name', '').strip()
        config_done = row.get('Config done (Y/N)', '').strip().upper()

        if not email_domain or not doc_id:
            continue

        if config_done == 'Y' or not config_done:
            new_mapping[email_domain] = {
                'docId': doc_id,
                'customerName': customer_name,
            }

        if not config_done and email_domain not in current_mapping:
            new_accounts.append(row)

    if new_mapping != current_mapping:
        save_mapping('account-mapping.json', new_mapping)
        print(f"Updated account mapping: {len(current_mapping)} -> {len(new_mapping)} accounts")
    else:
        print("Account mapping unchanged")

    results = []
    config_done_col = get_column_letter(headers, 'Config done (Y/N)')

    for row in new_accounts:
        email_domain = row.get('customer-email-domain', '').strip()
        customer_name = row.get('customer-name', '').strip()
        row_index = row['_row_index']

        print(f"New Gong account: {customer_name} ({email_domain})")

        status, err = _dispatch(
            GONG_SYNC_URL,
            {'full_backfill': 'true', 'account': email_domain},
            f"gong full_backfill {email_domain}",
        )
        results.append({
            'account': email_domain,
            'customer': customer_name,
            'status': status,
            'error': err,
        })

        if config_done_col and status == 'dispatched':
            cell_ref = f'{config_done_col}{row_index}'
            try:
                write_cell(SHEET_ID, GONG_TAB, cell_ref, 'Y')
                print(f"Marked {cell_ref} as Y")
            except Exception as e:
                print(f"Error marking config done for row {row_index}: {e}")

    return results


def fire_slack_drain():
    """Kick slack-sync's drain endpoint at the end of every run.

    Slack-sync buffers messages to GCS when the doc list is at cap.
    Once an operator extends the doc list (cap-hit runbook), those
    buffered messages need to be appended to the new tail. The drain
    endpoint is cheap when there's nothing to drain, so we fire it
    every run rather than gating on observed cap-hits.
    """
    return _dispatch(SLACK_SYNC_URL, {'drain': 'true'}, 'slack drain')


def config_sync(request):
    """Main Cloud Function entry point. Triggered hourly by Cloud Scheduler."""
    print(f"Config sync started at {datetime.now(timezone.utc).isoformat()}")

    slack_results = process_slack_tab()
    gong_results = process_gong_tab()
    drain_status, _ = fire_slack_drain()

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
        'slack_drain': drain_status,
    }

    print(f"Config sync complete: {json.dumps(result)}")
    return result, 200


if __name__ == '__main__':
    from flask import Flask, request
    app = Flask(__name__)

    @app.route('/', methods=['GET'])
    def handle():
        return config_sync(request)

    port = int(os.environ.get('PORT', 3002))
    print(f'Config sync server running on port {port}')
    app.run(host='0.0.0.0', port=port)
