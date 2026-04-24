"""SendGrid wrapper for doc-cap-hit operator alerts.

The alert is a plain-text email to `ALERT_EMAIL` saying "customer X's
doc list is full, calls/messages are being buffered to GCS until you
add a new doc id to the sheet". The operator's runbook
(see ARCHITECTURE.md "Cap-hit runbook") covers the manual steps.

Hard contract: this module NEVER raises. SendGrid outage, missing
secret, rate limit, malformed `to` address - all surface as a logged
warning and a `False` return. Callers (gong-sync, drain workers)
treat the alert as best-effort: the data is already safe in GCS, the
email is just a nudge.

Frequency: callers pass an `alerted_customers` set keyed however
they like (domain for gong, channel id for slack). `send_doc_full_alert`
checks-and-adds atomically per call so an over-eager loop can't
spam the same customer twice in one run.
"""
import json
import os
from datetime import datetime, timezone

from shared.secrets import get_secret

ALERT_EMAIL = os.environ.get('ALERT_EMAIL', '')
SENDGRID_FROM = os.environ.get('SENDGRID_FROM', '')


def send_doc_full_alert(
    *,
    customer_label,
    customer_key,
    doc_ids,
    pending_count,
    service,
    alerted_customers=None,
):
    """Send (or skip) the doc-full alert. Returns True if a request was sent.

    Args:
      customer_label:  human-friendly name for the email body ("Acme")
      customer_key:    de-dup key for `alerted_customers` (domain or channel id)
      doc_ids:         list of currently-mapped doc ids (the operator
                       needs this so they know which doc to extend)
      pending_count:   how many items are currently buffered in GCS
      service:         "gong" or "slack" (shows up in the email subject)
      alerted_customers: optional set; if provided, this function
                       no-ops when `customer_key` is already in it,
                       and adds it before returning.

    Never raises. Logs and returns False on any failure.
    """
    if alerted_customers is not None:
        if customer_key in alerted_customers:
            return False
        # Add BEFORE sending so a SendGrid hang on this customer
        # doesn't generate a second attempt later in the same run.
        alerted_customers.add(customer_key)

    if not ALERT_EMAIL:
        print(f"[alerts] ALERT_EMAIL unset, skipping doc-full alert for {customer_label}")
        return False
    if not SENDGRID_FROM:
        print(f"[alerts] SENDGRID_FROM unset, skipping doc-full alert for {customer_label}")
        return False

    try:
        api_key = get_secret('sendgrid-api-key')
    except Exception as e:
        print(f"[alerts] Could not load sendgrid-api-key: {e}")
        return False

    subject = f"[notebooklm] {service} doc full for {customer_label}"
    body = (
        f"Customer: {customer_label} ({customer_key})\n"
        f"Service: {service}\n"
        f"Mapped doc ids: {', '.join(doc_ids) if doc_ids else '(none)'}\n"
        f"Pending items in GCS: {pending_count}\n"
        f"Detected at: {datetime.now(timezone.utc).isoformat()}\n\n"
        "The current doc has hit its size cap. New calls/messages are\n"
        "being buffered in GCS. Please:\n"
        "  1. Create a new Google Doc and share it with the service account.\n"
        "  2. Append the new doc id to the customer's row in the sheet,\n"
        "     comma-separated, e.g. 'doc-old,doc-new'.\n"
        "  3. The next config-sync run picks it up and the buffered items\n"
        "     drain automatically.\n"
    )

    payload = {
        'personalizations': [{'to': [{'email': ALERT_EMAIL}]}],
        'from': {'email': SENDGRID_FROM},
        'subject': subject,
        'content': [{'type': 'text/plain', 'value': body}],
    }

    try:
        # Local import so the module imports cleanly in environments
        # that haven't installed `requests` (tests, ruff).
        import requests
        resp = requests.post(
            'https://api.sendgrid.com/v3/mail/send',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            data=json.dumps(payload),
            timeout=10,
        )
        if resp.status_code >= 400:
            print(f"[alerts] SendGrid {resp.status_code}: {resp.text[:200]}")
            return False
        print(f"[alerts] Sent doc-full alert for {customer_label}")
        return True
    except Exception as e:
        print(f"[alerts] SendGrid request failed: {e}")
        return False
