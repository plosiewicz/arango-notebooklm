import os
import json
import hmac
import hashlib
import time
from datetime import datetime
from slack_sdk import WebClient
from google.cloud import storage
from google_docs import append_message_to_doc, get_doc_text

# GCS config for channel mapping
GCS_BUCKET = os.environ.get('CONFIG_BUCKET', 'slack-notebooklm-config')
GCS_MAPPING_BLOB = 'channel-mapping.json'

# Mapping cache (refreshed every 5 minutes)
_mapping_cache = None
_mapping_loaded_at = 0
CACHE_TTL = 300  # 5 minutes

# Initialize Slack client
slack = WebClient(token=os.environ.get('SLACK_BOT_TOKEN'))

# Cache for user info to avoid repeated API calls
user_cache = {}


def get_channel_mapping():
    """Load channel mapping from GCS with 5-minute cache."""
    global _mapping_cache, _mapping_loaded_at

    if _mapping_cache and (time.time() - _mapping_loaded_at < CACHE_TTL):
        return _mapping_cache

    try:
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(GCS_MAPPING_BLOB)
        _mapping_cache = json.loads(blob.download_as_text())
        _mapping_loaded_at = time.time()
        print(f"Loaded channel mapping from GCS ({len(_mapping_cache)} channels)")
        return _mapping_cache
    except Exception as e:
        print(f"Error loading channel mapping from GCS: {e}")
        # Fall back to local file if GCS fails
        if _mapping_cache:
            print("Using stale cache")
            return _mapping_cache
        try:
            with open('channel-mapping.json', 'r') as f:
                _mapping_cache = json.load(f)
                _mapping_loaded_at = time.time()
                print("Fell back to local channel-mapping.json")
                return _mapping_cache
        except Exception:
            return {}


def verify_slack_signature(request):
    """Verify that the request came from Slack"""
    signature = request.headers.get('X-Slack-Signature', '')
    timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
    body = request.get_data(as_text=True)

    # Check timestamp to prevent replay attacks (5 min window)
    if abs(time.time() - int(timestamp)) > 300:
        return False

    sig_basestring = f'v0:{timestamp}:{body}'
    my_signature = 'v0=' + hmac.new(
        os.environ.get('SLACK_SIGNING_SECRET', '').encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(my_signature, signature)


def get_user_name(user_id):
    """Get user's display name from Slack"""
    if user_id in user_cache:
        return user_cache[user_id]

    try:
        result = slack.users_info(user=user_id)
        profile = result['user']['profile']
        name = profile.get('display_name') or profile.get('real_name') or result['user'].get('name')
        user_cache[user_id] = name
        return name
    except Exception as e:
        print(f"Error fetching user {user_id}: {e}")
        return user_id  # Fallback to user ID


def format_timestamp(ts):
    """Format Slack timestamp for display"""
    dt = datetime.fromtimestamp(float(ts))
    return dt.strftime('%m/%d/%Y, %I:%M %p')


def backfill_channel(channel_id, mapping, oldest_ts):
    """
    Backfill historical messages from a Slack channel into the Google Doc.

    Args:
        channel_id: Slack channel ID
        mapping: Dict with 'docId' and 'customerName'
        oldest_ts: Unix timestamp string — fetch messages from this point forward

    Returns:
        Dict with backfill results
    """
    doc_id = mapping['docId']
    customer_name = mapping['customerName']

    print(f"Starting backfill for {customer_name} (channel {channel_id}) from {oldest_ts}")

    # Read existing doc text for dedup
    try:
        existing_text = get_doc_text(doc_id)
        print(f"Read existing doc text ({len(existing_text)} chars)")
    except Exception as e:
        print(f"Could not read doc for dedup, proceeding without: {e}")
        existing_text = ""

    # Fetch all messages from Slack using pagination
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

            # Check for more pages
            response_metadata = result.get('response_metadata', {})
            cursor = response_metadata.get('next_cursor')
            if not cursor:
                break

            # Small delay to respect rate limits
            time.sleep(0.5)

        except Exception as e:
            print(f"Error fetching history: {e}")
            break

    if not all_messages:
        print("No messages found in the specified range")
        return {"added": 0, "skipped": 0, "total_fetched": 0}

    # Sort oldest first (conversations.history returns newest first)
    all_messages.sort(key=lambda m: float(m.get('ts', '0')))

    print(f"Total messages fetched: {len(all_messages)}")

    # Filter to only real user messages (no subtypes like join/leave/bot)
    user_messages = [m for m in all_messages if not m.get('subtype')]
    print(f"User messages (excluding system/bot): {len(user_messages)}")

    # Build batch of new messages, deduplicating against existing doc text
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

        # Dedup: check if this message's header line already exists in the doc
        dedup_key = f"[{timestamp}] {user_name}:"
        if dedup_key in existing_text:
            skipped += 1
            continue

        formatted_message = f"[{timestamp}] {user_name}:\n{text}\n\n"
        batch_text.append(formatted_message)
        added += 1

    if batch_text:
        # Batch-append all messages in a single API call
        full_text = ''.join(batch_text)
        print(f"Appending {added} messages ({len(full_text)} chars) to doc {doc_id}")

        from google_docs import get_docs_client
        docs = get_docs_client()
        doc = docs.documents().get(documentId=doc_id).execute()
        end_index = doc['body']['content'][-1]['endIndex'] - 1

        docs.documents().batchUpdate(
            documentId=doc_id,
            body={
                'requests': [
                    {
                        'insertText': {
                            'location': {'index': end_index},
                            'text': full_text
                        }
                    }
                ]
            }
        ).execute()

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
    """Handle a backfill request (GET ?backfill=true)."""
    args = request.args if hasattr(request, 'args') else {}

    channel_id = args.get('channel')
    # Default oldest: Jan 1 2024 00:00:00 UTC
    oldest_ts = args.get('oldest', '1704067200')

    channel_mapping = get_channel_mapping()

    if channel_id:
        # Backfill a specific channel
        mapping = channel_mapping.get(channel_id)
        if not mapping:
            return {"error": f"No mapping found for channel {channel_id}"}, 404

        result = backfill_channel(channel_id, mapping, oldest_ts)
        return result, 200
    else:
        # Backfill all mapped channels
        results = []
        for ch_id, mapping in channel_mapping.items():
            try:
                result = backfill_channel(ch_id, mapping, oldest_ts)
                results.append(result)
            except Exception as e:
                results.append({"channel": ch_id, "error": str(e)})
        return {"results": results}, 200


def slack_webhook(request):
    """Main entry point for Google Cloud Functions.

    Routes:
    - GET with ?backfill=true → backfill handler
    - POST → Slack webhook handler
    """
    # Handle backfill requests (GET)
    if request.method == 'GET':
        args = request.args if hasattr(request, 'args') else {}
        if args.get('backfill', '').lower() == 'true':
            return handle_backfill(request)
        return 'Slack NotebookLM Sync is running.', 200

    # --- Slack webhook handling (POST) ---

    # Slack retries if it doesn't get a response within 3 seconds.
    # Ignore retries to prevent duplicate messages.
    retry_num = request.headers.get('X-Slack-Retry-Num')
    if retry_num:
        print(f'Ignoring Slack retry #{retry_num}')
        return 'OK', 200

    # Parse the request body
    body = request.get_json(silent=True) or {}

    # Log all incoming requests for debugging
    print(f"Received request: {json.dumps(body)}")

    # Handle Slack URL verification challenge
    if body.get('type') == 'url_verification':
        print('Received URL verification challenge')
        return {'challenge': body.get('challenge')}, 200

    # Verify request signature (skip for health checks and dev)
    if body and body.get('type') != 'url_verification' and os.environ.get('VERIFY_SIGNATURE') != 'false':
        try:
            if not verify_slack_signature(request):
                print('Invalid signature')
                return 'Invalid signature', 401
        except Exception as e:
            print(f'Signature verification error: {e}')
            # Continue anyway in case of verification issues during setup

    # Process the event
    try:
        event = body.get('event', {})
        channel_mapping = get_channel_mapping()

        # Only process message events (not subtypes like message_changed, etc.)
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
                text=text
            )

            print(f"Successfully appended message to doc {mapping['docId']}")

    except Exception as e:
        print(f'Error processing event: {e}')

    return 'OK', 200


# For local testing
if __name__ == '__main__':
    from flask import Flask, request
    app = Flask(__name__)

    @app.route('/', methods=['GET', 'POST'])
    def handle():
        return slack_webhook(request)

    port = int(os.environ.get('PORT', 3000))
    print(f'Server running on port {port}')
    app.run(host='0.0.0.0', port=port)
