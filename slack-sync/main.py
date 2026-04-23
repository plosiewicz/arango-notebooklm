"""Slack -> Google Docs sync Cloud Function.

Two entry points, same handler:
  POST /            Slack Events API webhook (new message -> append to doc)
  GET  /?backfill=true&channel=<id>&oldest=<ts>
                    One-shot backfill of historical messages for a channel.

Channel -> doc routing comes from the GCS mapping blob (config-sync
is the writer). Secrets come from GCP Secret Manager.
"""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime

from slack_sdk import WebClient

from shared.gcs_mapping import load_mapping
from shared.google_docs import append_to_doc, get_doc_text
from shared.secrets import get_secret

MAPPING_BLOB = 'channel-mapping.json'

_slack_client = None
_user_cache = {}


def get_slack_client():
    """Lazily build the Slack client with the bot token from Secret Manager."""
    global _slack_client
    if _slack_client is None:
        _slack_client = WebClient(token=get_secret('slack-bot-token'))
    return _slack_client


def get_channel_mapping():
    return load_mapping(MAPPING_BLOB)


def verify_slack_signature(request):
    """Return True iff the request HMAC matches our signing secret.

    Uses Slack's v0 scheme with a 5 minute replay window. Any failure
    (bad/missing headers, bad timestamp, bad signature, Secret Manager
    miss) returns False - we never let a request through unauthenticated.
    """
    signature = request.headers.get('X-Slack-Signature', '')
    timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
    body = request.get_data(as_text=True)

    if not signature or not timestamp:
        return False

    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False

    try:
        signing_secret = get_secret('slack-signing-secret')
    except Exception as e:
        # Fail closed: if we can't load the signing secret (Secret Manager
        # outage, IAM misconfig, missing version) we must reject the
        # request rather than let it through unauthenticated.
        print(f'Failed to load slack signing secret: {e}')
        return False

    sig_basestring = f'v0:{timestamp}:{body}'
    my_signature = 'v0=' + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(my_signature, signature)


def get_user_name(user_id):
    """Return the best available display name for a Slack user id, cached."""
    if user_id in _user_cache:
        return _user_cache[user_id]

    try:
        result = get_slack_client().users_info(user=user_id)
        profile = result['user']['profile']
        name = (
            profile.get('display_name')
            or profile.get('real_name')
            or result['user'].get('name')
        )
        _user_cache[user_id] = name
        return name
    except Exception as e:
        print(f"Error fetching user {user_id}: {e}")
        return user_id


def format_timestamp(ts):
    return datetime.fromtimestamp(float(ts)).strftime('%m/%d/%Y, %I:%M %p')


def format_message(user_name, timestamp, text):
    return f"[{timestamp}] {user_name}:\n{text}\n\n"


def append_message_to_doc(doc_id, user_name, timestamp, text):
    append_to_doc(doc_id, format_message(user_name, timestamp, text))


def backfill_channel(channel_id, mapping, oldest_ts):
    """Page through a channel's history and append new user messages to the doc.

    Dedups against the doc itself by looking for the "[ts] user:" header,
    so reruns are idempotent. Batches all new messages into a single
    append call.
    """
    doc_id = mapping['docId']
    customer_name = mapping['customerName']
    slack = get_slack_client()

    print(f"Starting backfill for {customer_name} (channel {channel_id}) from {oldest_ts}")

    try:
        existing_text = get_doc_text(doc_id)
        print(f"Read existing doc text ({len(existing_text)} chars)")
    except Exception as e:
        print(f"Could not read doc for dedup, proceeding without: {e}")
        existing_text = ""

    all_messages = []
    cursor = None

    while True:
        try:
            kwargs = {
                'channel': channel_id,
                'oldest': str(oldest_ts),
                'limit': 200,
                'inclusive': True,
            }
            if cursor:
                kwargs['cursor'] = cursor

            result = slack.conversations_history(**kwargs)
            messages = result.get('messages', [])
            all_messages.extend(messages)
            print(f"Fetched {len(all_messages)} messages so far...")

            cursor = result.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                break

            time.sleep(0.5)
        except Exception as e:
            print(f"Error fetching history: {e}")
            break

    if not all_messages:
        print("No messages found in the specified range")
        return {"added": 0, "skipped": 0, "total_fetched": 0}

    all_messages.sort(key=lambda m: float(m.get('ts', '0')))
    user_messages = [m for m in all_messages if not m.get('subtype')]
    print(f"Total fetched: {len(all_messages)}, user messages: {len(user_messages)}")

    added = 0
    skipped = 0
    batch_text = []

    for msg in user_messages:
        user_id = msg.get('user')
        ts = msg.get('ts')
        text = msg.get('text', '')

        if not user_id or not ts:
            skipped += 1
            continue

        user_name = get_user_name(user_id)
        timestamp = format_timestamp(ts)

        dedup_key = f"[{timestamp}] {user_name}:"
        if dedup_key in existing_text:
            skipped += 1
            continue

        batch_text.append(format_message(user_name, timestamp, text))
        added += 1

    if batch_text:
        full_text = ''.join(batch_text)
        print(f"Appending {added} messages ({len(full_text)} chars) to doc {doc_id}")
        append_to_doc(doc_id, full_text)

    result = {
        "channel": channel_id,
        "customer": customer_name,
        "added": added,
        "skipped": skipped,
        "total_fetched": len(all_messages),
    }
    print(f"Backfill complete: {result}")
    return result


def handle_backfill(request):
    args = request.args if hasattr(request, 'args') else {}
    channel_id = args.get('channel')
    # Default oldest: Jan 1 2024 00:00:00 UTC
    oldest_ts = args.get('oldest', '1704067200')

    channel_mapping = get_channel_mapping()

    if channel_id:
        mapping = channel_mapping.get(channel_id)
        if not mapping:
            return {"error": f"No mapping found for channel {channel_id}"}, 404
        return backfill_channel(channel_id, mapping, oldest_ts), 200

    results = []
    for ch_id, mapping in channel_mapping.items():
        try:
            results.append(backfill_channel(ch_id, mapping, oldest_ts))
        except Exception as e:
            results.append({"channel": ch_id, "error": str(e)})
    return {"results": results}, 200


def slack_webhook(request):
    """Cloud Function entry point.

    GET ?backfill=true -> handle_backfill
    GET (else)         -> health check
    POST               -> Slack Events API webhook
    """
    if request.method == 'GET':
        args = request.args if hasattr(request, 'args') else {}
        if args.get('backfill', '').lower() == 'true':
            return handle_backfill(request)
        return 'Slack NotebookLM Sync is running.', 200

    # Slack retries if it doesn't see a 200 within 3s. Drop retries so we
    # don't double-append the same message.
    retry_num = request.headers.get('X-Slack-Retry-Num')
    if retry_num:
        print(f'Ignoring Slack retry #{retry_num}')
        return 'OK', 200

    body = request.get_json(silent=True) or {}
    print(f"Received request: {json.dumps(body)}")

    if body.get('type') == 'url_verification':
        print('Received URL verification challenge')
        return {'challenge': body.get('challenge')}, 200

    if not verify_slack_signature(request):
        print('Invalid signature')
        return 'Invalid signature', 401

    try:
        event = body.get('event', {})
        channel_mapping = get_channel_mapping()

        if event.get('type') == 'message' and not event.get('subtype'):
            channel_id = event.get('channel')
            mapping = channel_mapping.get(channel_id)

            if not mapping:
                print(f'No mapping found for channel {channel_id}')
                return 'OK', 200

            user_name = get_user_name(event.get('user'))
            timestamp = format_timestamp(event.get('ts'))
            text = event.get('text', '')

            print(f"Processing message from {user_name} in {mapping['customerName']}")
            append_message_to_doc(
                doc_id=mapping['docId'],
                user_name=user_name,
                timestamp=timestamp,
                text=text,
            )
            print(f"Successfully appended message to doc {mapping['docId']}")

    except Exception as e:
        print(f'Error processing event: {e}')

    return 'OK', 200


if __name__ == '__main__':
    from flask import Flask, request
    app = Flask(__name__)

    @app.route('/', methods=['GET', 'POST'])
    def handle():
        return slack_webhook(request)

    port = int(os.environ.get('PORT', 3000))
    print(f'Server running on port {port}')
    app.run(host='0.0.0.0', port=port)
