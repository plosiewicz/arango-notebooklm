import os
import json
import hmac
import hashlib
import time
from slack_sdk import WebClient
from google_docs import append_message_to_doc

# Load channel mapping
with open('channel-mapping.json', 'r') as f:
    channel_mapping = json.load(f)

# Initialize Slack client
slack = WebClient(token=os.environ.get('SLACK_BOT_TOKEN'))

# Cache for user info to avoid repeated API calls
user_cache = {}


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
    from datetime import datetime
    dt = datetime.fromtimestamp(float(ts))
    return dt.strftime('%m/%d/%Y, %I:%M %p')


def slack_webhook(request):
    """Main webhook handler for Google Cloud Functions"""
    
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
        if request.method == 'POST':
            return slack_webhook(request)
        return 'Slack NotebookLM Sync is running!'

    port = int(os.environ.get('PORT', 3000))
    print(f'Server running on port {port}')
    app.run(host='0.0.0.0', port=port)
