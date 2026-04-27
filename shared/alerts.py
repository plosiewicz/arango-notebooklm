"""Doc-cap-hit operator alert.

Emits a single structured `doc_full` warning whenever the tail Google
Doc for a customer hits its size cap and incoming items are being
buffered to GCS instead. A Cloud Monitoring log-based alert filters
on `jsonPayload.event="doc_full"` and routes the match to the
operator's email notification channel; this module knows nothing
about email or Cloud Monitoring -- it just logs.

On Cloud Functions Gen 2 (current target -- see deploy.sh `--gen2`)
the `google-cloud-logging` Python handler is auto-attached and
`extra={"json_fields": {...}}` is hoisted to `jsonPayload.*` for the
log entry. In local `functions-framework` dev or other stdlib-only
contexts the same line still appears as a plain WARNING with the
key fields embedded in the message string, so it remains searchable.

Hard contract: this module NEVER raises. A logging-stack hiccup
falls back to a `print()` ops note; callers (gong-sync, drain
workers, slack-sync backfill / drain) treat the alert as
best-effort. The data is already safe in GCS; the alert is just a
nudge.

Frequency: callers pass an `alerted_customers` set keyed however
they like (domain for gong, channel id for slack).
`send_doc_full_alert` checks-and-adds atomically per call so an
over-eager loop can't spam the same customer twice in one run. The
cross-run side -- "operator gets one ongoing email per cap-hit
customer until acked" -- is owned by the alert policy's auto-close
window, not by this module.
"""
import logging

logger = logging.getLogger(__name__)


def send_doc_full_alert(
    *,
    customer_label,
    customer_key,
    doc_ids,
    pending_count,
    service,
    alerted_customers=None,
):
    """Emit (or skip) the doc-full alert log line.

    Returns True if a record was emitted, False if it was deduped or
    the logging call itself failed.

    Args:
      customer_label:  human-friendly name ("Acme") for the message
      customer_key:    de-dup key for `alerted_customers`
                       (domain for gong, channel id for slack)
      doc_ids:         list of currently-mapped doc ids
      pending_count:   how many items are currently buffered in GCS
      service:         "gong" or "slack"
      alerted_customers: optional set; if provided, the function
                       no-ops when `customer_key` is already in it,
                       and adds it BEFORE emitting so a logging hang
                       on this customer can't re-enter the same run.

    Never raises.
    """
    if alerted_customers is not None:
        if customer_key in alerted_customers:
            return False
        alerted_customers.add(customer_key)

    doc_ids_list = list(doc_ids or [])

    try:
        logger.warning(
            "doc_full service=%s customer=%s key=%s doc_ids=%s pending=%d",
            service,
            customer_label,
            customer_key,
            doc_ids_list,
            pending_count,
            extra={"json_fields": {
                "event": "doc_full",
                "service": service,
                "customer_label": customer_label,
                "customer_key": customer_key,
                "doc_ids": doc_ids_list,
                "pending_count": pending_count,
            }},
        )
        return True
    except Exception as e:
        print(f"[alerts] log emit failed: {e}")
        return False
