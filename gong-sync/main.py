"""Gong -> Google Docs sync Cloud Function.

Triggered hourly by Cloud Scheduler for incremental sync; also
supports ad-hoc backfill via query params:

  GET /                      no-op body, same as ?hours=...
  GET /?hours=2              normal mode, look back N hours (default 2)
  GET /?backfill=true&days=N backfill mode, last N days (default 90)
  GET /?account=<key>        restrict to a single account (email domain,
                             name, or id) - useful for debugging

Account -> doc routing comes from the GCS mapping blob (config-sync is
the writer). Dedup is content-based: we read the target doc once per
account and skip any call whose "GONG CALL: <title>" + formatted date
already appears.
"""
import os
from datetime import datetime, timedelta, timezone

from gong_api import (
    format_transcript,
    get_account_info_from_call,
    get_call_details,
    get_calls_in_range,
    get_calls_since,
    get_transcript,
)
from shared.gcs_mapping import load_mapping
from shared.google_docs import append_to_doc, get_doc_text

MAPPING_BLOB = 'account-mapping.json'


def get_account_mapping():
    return load_mapping(MAPPING_BLOB)


def find_mapping_for_account(account_id, account_name):
    """Return the mapping for an account, matching id first then name (case-insensitive)."""
    account_mapping = get_account_mapping()

    if account_id and account_id in account_mapping:
        return account_mapping[account_id]
    if account_name and account_name in account_mapping:
        return account_mapping[account_name]
    if account_name:
        account_lower = account_name.lower()
        for key, value in account_mapping.items():
            if key.lower() == account_lower:
                return value
    return None


def format_call_for_doc(call_details, transcript, summary):
    """Render a Gong call into the doc-ready text block we append.

    The "GONG CALL: <title>" header + formatted date is what our
    content-based dedup keys off of, so don't change that format without
    also updating the dedup logic in process_calls.
    """
    title = call_details.get("title", "Untitled Call")
    date = call_details.get("started", "Unknown date")
    duration_minutes = call_details.get("duration", 0) // 60

    participants = []
    for party in call_details.get("parties", []):
        name = party.get("name", "Unknown")
        company = party.get("company", "")
        participants.append(f"{name} ({company})" if company else name)
    participants_str = ", ".join(participants) if participants else "Unknown"

    if date and date != "Unknown date":
        try:
            dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
            date = dt.strftime("%B %d, %Y at %I:%M %p")
        except Exception:
            pass

    return f"""
=====================================
GONG CALL: {title}
=====================================
Date: {date}
Duration: {duration_minutes} minutes
Participants: {participants_str}
=====================================

## AI Summary

{summary if summary else "No summary available."}

## Full Transcript

{transcript}

---

"""


def process_calls(calls, account_filter=None):
    """Fetch details + transcripts for `calls` and append each to its doc.

    Dedup is content-based (reads the doc once, caches the text, checks
    for the call's header line). `account_filter` restricts processing
    to a single account - non-matching calls are silently skipped.

    Returns (processed_count, errors, skipped_accounts, skipped_dupes).
    """
    if not calls:
        return (0, [], {}, 0)

    print(f"Processing {len(calls)} calls" + (f" (filter: {account_filter})" if account_filter else ""))

    call_ids = [c.get("id") for c in calls]
    all_detailed = []
    for i in range(0, len(call_ids), 100):
        batch = call_ids[i:i + 100]
        detailed = get_call_details(batch)
        all_detailed.extend(detailed)
        print(f"Fetched details batch {i // 100 + 1}: {len(detailed)} / {len(batch)} calls")

    call_details_map = {c.get("metaData", {}).get("id"): c for c in all_detailed}

    processed_count = 0
    skipped_dupes = 0
    errors = []
    skipped_accounts = {}
    doc_text_cache = {}

    for call in calls:
        call_id = call.get("id")

        try:
            details = call_details_map.get(call_id, {})

            account_id, account_name = get_account_info_from_call(details)
            if not account_id and not account_name:
                print(f"Could not determine account for call {call_id}, skipping")
                continue

            if account_filter:
                filter_lower = account_filter.lower()
                match = (
                    (account_id and account_id.lower() == filter_lower)
                    or (account_name and account_name.lower() == filter_lower)
                )
                if not match:
                    continue

            mapping = find_mapping_for_account(account_id, account_name)
            if not mapping:
                key = account_name or account_id
                skipped_accounts[key] = skipped_accounts.get(key, 0) + 1
                print(f"No mapping found for account '{account_name}' (ID: {account_id}), skipping")
                continue

            doc_id = mapping.get("docId")

            if doc_id not in doc_text_cache:
                try:
                    doc_text_cache[doc_id] = get_doc_text(doc_id)
                    print(f"Cached doc text for {doc_id} ({len(doc_text_cache[doc_id])} chars)")
                except Exception as e:
                    print(f"Could not read doc {doc_id} for dedup, proceeding without: {e}")
                    doc_text_cache[doc_id] = ""

            call_title = details.get("metaData", {}).get("title", "Untitled Call")
            call_started = details.get("metaData", {}).get("started", "")
            dedup_date = ""
            if call_started:
                try:
                    dt = datetime.fromisoformat(call_started.replace("Z", "+00:00"))
                    dedup_date = dt.strftime("%B %d, %Y at %I:%M %p")
                except Exception:
                    dedup_date = call_started

            dedup_key = f"GONG CALL: {call_title}"
            if dedup_key in doc_text_cache[doc_id] and dedup_date and dedup_date in doc_text_cache[doc_id]:
                print(f"Skipping duplicate call '{call_title}' already in doc {doc_id}")
                skipped_dupes += 1
                continue

            transcript_entries = get_transcript(call_id)

            parties = details.get("parties", [])
            participants = {p.get("speakerId"): p for p in parties}
            formatted_transcript = format_transcript(transcript_entries, participants)

            summary = details.get("content", {}).get("brief")

            call_info = {
                "title": call_title,
                "started": details.get("metaData", {}).get("started"),
                "duration": details.get("metaData", {}).get("duration", 0),
                "parties": parties,
            }

            doc_content = format_call_for_doc(call_info, formatted_transcript, summary)
            append_to_doc(doc_id, doc_content)

            # Keep the cache in sync so later calls in this run also dedup.
            doc_text_cache[doc_id] += doc_content

            print(f"Successfully synced call '{call_title}' for '{account_name}'")
            processed_count += 1

        except Exception as e:
            error_msg = f"Error processing call {call_id}: {e}"
            print(error_msg)
            errors.append(error_msg)

    if skipped_dupes:
        print(f"Skipped {skipped_dupes} duplicate calls already in docs")

    return (processed_count, errors, skipped_accounts, skipped_dupes)


def gong_sync(request):
    """Cloud Function entry point.

    Query params:
      backfill=true    pull calls from the last `days` days (default 90)
      hours=N          normal mode: pull calls from the last N hours (default 2)
      days=N           only used when backfill=true (default 90)
      account=<key>    restrict processing to one account key
    """
    args = request.args if hasattr(request, 'args') else {}

    backfill_mode = args.get('backfill', 'false').lower() == 'true'
    account_filter = args.get('account', '').strip() or None

    print(f"Starting Gong sync at {datetime.now(timezone.utc).isoformat()} (backfill={backfill_mode})")
    if account_filter:
        print(f"Account filter: {account_filter}")

    if backfill_mode:
        days = int(args.get('days', 90))
        print(f"Backfill: fetching calls from the last {days} days")
        to_date = datetime.now(timezone.utc)
        from_date = to_date - timedelta(days=days)
        calls = get_calls_in_range(from_date, to_date)
    else:
        hours = int(args.get('hours', 2))
        print(f"Normal mode: fetching calls from the last {hours} hours")
        calls = get_calls_since(hours_ago=hours)

    if not calls:
        print("No calls found in the specified time range.")
        return {"message": "No calls to process", "processed": 0}, 200

    print(f"Found {len(calls)} calls to process")

    processed_count, errors, skipped_accounts, skipped_dupes = process_calls(calls, account_filter)

    result = {
        "message": f"Processed {processed_count} calls",
        "processed": processed_count,
        "skipped_dupes": skipped_dupes,
        "total_found": len(calls),
        "skipped_accounts": skipped_accounts or None,
        "errors": errors or None,
    }

    print(f"Gong sync complete: {result}")
    return result, 200


if __name__ == '__main__':
    from flask import Flask, request
    app = Flask(__name__)

    @app.route('/', methods=['GET', 'POST'])
    def handle():
        return gong_sync(request)

    port = int(os.environ.get('PORT', 3001))
    print(f'Server running on port {port}')
    app.run(host='0.0.0.0', port=port)
