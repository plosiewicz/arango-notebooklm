"""Slack -> Google Docs sync Cloud Function.

Two entry points, same handler:
  POST /            Slack Events API webhook (new message -> append to doc)
  GET  /?backfill=true&channel=<id>[&oldest=<ts>]
                    One-shot backfill of historical messages for a channel.
                    `oldest` defaults to the channel's `created` timestamp
                    so a fresh onboarding captures the entire history.
  GET  /?drain=true Drain `pending-messages/<channel-id>/` (best-effort
                    flush of messages that hit a doc-cap during a webhook
                    or backfill). config-sync fires this every hour.

Channel -> doc routing comes from the GCS mapping blob (config-sync
is the writer). Cells are parsed via `shared.sheets.parse_id_list`,
so a customer with a doc that hit the cap can be extended to a list
like `doc-old,doc-new`. New messages always land on the LAST id in
that list; dedup runs against the concatenation of all docs in the
list so a webhook never re-appends a message that's already in an
older doc.

Cap-hit on the webhook hot path:

The Slack Events API retries if it doesn't see a 200 within 3 seconds.
SendGrid + concatenated multi-doc reads can each blow that budget, so
the webhook keeps a small in-process cache keyed by channel_id and
SKIPS the alert call - it just buffers the formatted message to GCS
and returns. The hourly drain (kicked by config-sync) is what fires
the alert and actually empties the queue.
"""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime

from slack_sdk import WebClient

from shared import alerts, pending
from shared.gcs_mapping import load_mapping
from shared.google_docs import DocFullError, append_to_doc, get_doc_text
from shared.secrets import get_secret
from shared.sheets import parse_id_list

MAPPING_BLOB = 'channel-mapping.json'

# Per-process caches. _user_cache survives across invocations on a
# warm container; the doc-text cache below is rebuilt on each webhook
# whenever the doc list changes (so an extended doc list invalidates
# itself the moment the operator updates the sheet).
_slack_client = None
_user_cache = {}

# channel_id -> (tuple(doc_ids), concatenated_doc_text)
# Webhook-only; backfill and drain build their own short-lived caches.
_webhook_doc_cache = {}


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


def _read_concatenated_text(doc_ids):
    """Return the concatenation of every doc's plaintext.

    Read failures don't poison the dedup pass: we cache an empty
    string for the unreadable doc and proceed, matching the historical
    "best-effort dedup" behaviour we used for the single-doc case.
    """
    parts = []
    for doc_id in doc_ids:
        try:
            parts.append(get_doc_text(doc_id))
        except Exception as e:
            print(f"Could not read doc {doc_id} for dedup, proceeding without: {e}")
            parts.append("")
    return "".join(parts)


def _get_channel_created_ts(channel_id):
    """Return the channel's `created` unix ts, or None on any failure.

    Used as the default `oldest` for backfill so a fresh onboarding
    captures the entire channel history without the sheet needing a
    'Backlog through' column.
    """
    try:
        info = get_slack_client().conversations_info(channel=channel_id)
        return int(info['channel']['created'])
    except Exception as e:
        print(f"conversations.info({channel_id}) failed: {e}")
        return None


def backfill_channel(channel_id, mapping, oldest_ts=None):
    """Page through a channel's history and append new user messages to the doc list.

    Multi-doc dedup: scans the concatenated text of every doc in
    mapping['docId'] (parse_id_list). Cap-aware: on DocFullError the
    remainder of the run's batch is buffered to GCS and a single alert
    is fired. `oldest_ts` defaults to channel.created so a fresh
    onboarding captures the entire history.
    """
    customer_name = mapping.get('customerName', '')
    doc_ids = parse_id_list(mapping.get('docId', ''))
    if not doc_ids:
        return {"channel": channel_id, "error": "empty docId in mapping"}

    if oldest_ts is None:
        oldest_ts = _get_channel_created_ts(channel_id) or 0

    slack = get_slack_client()
    print(f"Starting backfill for {customer_name} (channel {channel_id}) from {oldest_ts}")

    existing_text = _read_concatenated_text(doc_ids)
    print(f"Read existing doc text from {len(doc_ids)} doc(s) ({len(existing_text)} chars)")

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
        return {"channel": channel_id, "added": 0, "skipped": 0, "buffered": 0, "total_fetched": 0}

    all_messages.sort(key=lambda m: float(m.get('ts', '0')))
    user_messages = [m for m in all_messages if not m.get('subtype')]
    print(f"Total fetched: {len(all_messages)}, user messages: {len(user_messages)}")

    added = 0
    skipped = 0
    buffered = 0
    cap_hit = False
    alerted = set()

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

        block = format_message(user_name, timestamp, text)

        # If a previous message in this run already hit the cap, every
        # subsequent message goes straight to the queue - re-trying the
        # append is just a guaranteed second cap hit on the same doc.
        if cap_hit:
            try:
                pending.enqueue(
                    pending.PREFIX_SLACK, channel_id, block,
                    meta={'ts': ts, 'user': user_name},
                    unique_id=ts,
                )
                buffered += 1
            except Exception as e:
                print(f"Failed to buffer message {ts}: {e}")
            continue

        try:
            append_to_doc(
                doc_ids, block,
                current_text_bytes=len(existing_text.encode('utf-8')),
            )
            existing_text += block
            added += 1
        except DocFullError as e:
            print(f"Backfill hit cap on {channel_id} (doc {e.doc_id}); buffering remainder")
            cap_hit = True
            try:
                pending.enqueue(
                    pending.PREFIX_SLACK, channel_id, block,
                    meta={'ts': ts, 'user': user_name},
                    unique_id=ts,
                )
                buffered += 1
            except Exception as enqueue_err:
                print(f"Failed to buffer message {ts}: {enqueue_err}")
            pending_count = pending.count(pending.PREFIX_SLACK, channel_id)
            alerts.send_doc_full_alert(
                customer_label=customer_name or channel_id,
                customer_key=channel_id,
                doc_ids=doc_ids,
                pending_count=pending_count,
                service='slack',
                alerted_customers=alerted,
            )

    result = {
        "channel": channel_id,
        "customer": customer_name,
        "added": added,
        "skipped": skipped,
        "buffered": buffered,
        "total_fetched": len(all_messages),
    }
    print(f"Backfill complete: {result}")
    return result


def drain_channel(channel_id, mapping):
    """Drain `pending-messages/<channel_id>/` to the customer's doc list.

    Stops on the first DocFullError per channel - subsequent items
    would just hit the same wall. Fires one SendGrid alert if a
    cap-hit is observed (this path is NOT the webhook hot path so
    we don't have a 3-second budget).
    """
    doc_ids = parse_id_list(mapping.get('docId', ''))
    if not doc_ids:
        return {"channel": channel_id, "drained": 0, "error": "empty docId"}

    existing_text = _read_concatenated_text(doc_ids)
    drained = 0
    alerted = set()

    for key, payload in pending.drain(pending.PREFIX_SLACK, channel_id):
        content = payload.get('content', '')
        if not content:
            pending.delete(pending.PREFIX_SLACK, channel_id, key)
            continue
        try:
            append_to_doc(
                doc_ids, content,
                current_text_bytes=len(existing_text.encode('utf-8')),
            )
            existing_text += content
            pending.delete(pending.PREFIX_SLACK, channel_id, key)
            drained += 1
        except DocFullError as e:
            print(f"Drain hit cap on {channel_id} (doc {e.doc_id})")
            pending_count = pending.count(pending.PREFIX_SLACK, channel_id)
            alerts.send_doc_full_alert(
                customer_label=mapping.get('customerName') or channel_id,
                customer_key=channel_id,
                doc_ids=doc_ids,
                pending_count=pending_count,
                service='slack',
                alerted_customers=alerted,
            )
            break
        except Exception as e:
            print(f"Drain failed for pending {key} on {channel_id}: {e}")
            continue

    return {"channel": channel_id, "drained": drained}


def handle_drain(_request):
    """Drain every `pending-messages/<partition>/` we can find.

    Channels whose mapping has been removed are left in the queue so
    the operator notices.
    """
    try:
        partitions = pending.list_partitions(pending.PREFIX_SLACK)
    except Exception as e:
        return {"error": f"could not list partitions: {e}"}, 200

    if not partitions:
        return {"drained": 0, "channels": []}, 200

    mapping = get_channel_mapping()
    results = []
    total = 0
    for channel_id in sorted(partitions):
        entry = mapping.get(channel_id)
        if not entry:
            print(f"Pending partition '{channel_id}' has no current mapping, skipping")
            continue
        out = drain_channel(channel_id, entry)
        total += out.get('drained', 0)
        results.append(out)

    return {"drained": total, "channels": results}, 200


def handle_backfill(request):
    args = request.args if hasattr(request, 'args') else {}
    channel_id = args.get('channel')
    oldest_arg = args.get('oldest')
    oldest_ts = int(oldest_arg) if oldest_arg else None

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


def _webhook_dedup_text(channel_id, doc_ids):
    """Return cached concatenated doc text for `channel_id`, refreshing
    on first sight or when the doc-id list has changed.

    Cache key is the tuple of doc ids so an operator extending the
    list (`doc-old,doc-new`) invalidates the cache automatically on
    the next webhook.
    """
    cached = _webhook_doc_cache.get(channel_id)
    sig = tuple(doc_ids)
    if cached and cached[0] == sig:
        return cached[1]
    text = _read_concatenated_text(doc_ids)
    _webhook_doc_cache[channel_id] = (sig, text)
    return text


def _handle_webhook_message(event, mapping):
    """Append (or buffer) a single Slack message. Webhook-budget aware:
    no SendGrid alerts on this path - the hourly drain owns alerting.
    """
    channel_id = event.get('channel')
    user_id = event.get('user')
    ts = event.get('ts')
    text = event.get('text', '')
    if not user_id or not ts or not channel_id:
        return

    doc_ids = parse_id_list(mapping.get('docId', ''))
    if not doc_ids:
        print(f"Mapping for channel {channel_id} has empty docId, skipping")
        return

    user_name = get_user_name(user_id)
    timestamp = format_timestamp(ts)

    dedup_text = _webhook_dedup_text(channel_id, doc_ids)
    dedup_key = f"[{timestamp}] {user_name}:"
    if dedup_key in dedup_text:
        print(f"Skipping duplicate webhook message in {channel_id}")
        return

    block = format_message(user_name, timestamp, text)

    try:
        append_to_doc(
            doc_ids, block,
            current_text_bytes=len(dedup_text.encode('utf-8')),
        )
    except DocFullError as e:
        # Hot path: do NOT send the email here. The hourly drain
        # (kicked by config-sync) will alert when it can't make
        # progress. This buffer-only path keeps us under Slack's 3s
        # retry budget.
        print(f"Webhook hit cap on {channel_id} (doc {e.doc_id}); buffering")
        try:
            pending.enqueue(
                pending.PREFIX_SLACK, channel_id, block,
                meta={'ts': ts, 'user': user_name},
                unique_id=ts,
            )
        except Exception as enqueue_err:
            print(f"Failed to buffer webhook message {ts}: {enqueue_err}")
        return

    # Update the cache with the new tail content so the next webhook
    # in the same warm container dedups correctly without a re-read.
    _webhook_doc_cache[channel_id] = (
        tuple(doc_ids),
        dedup_text + block,
    )


def slack_webhook(request):
    """Cloud Function entry point.

    GET ?backfill=true -> handle_backfill
    GET ?drain=true    -> handle_drain
    GET (else)         -> health check
    POST               -> Slack Events API webhook
    """
    if request.method == 'GET':
        args = request.args if hasattr(request, 'args') else {}
        if args.get('backfill', '').lower() == 'true':
            return handle_backfill(request)
        if args.get('drain', '').lower() == 'true':
            return handle_drain(request)
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

            print(f"Processing message in {mapping.get('customerName')}")
            _handle_webhook_message(event, mapping)

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
