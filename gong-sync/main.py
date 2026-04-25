"""Gong -> Google Docs sync Cloud Function.

Triggered hourly by Cloud Scheduler for incremental sync; also
supports ad-hoc backfill via query params:

  GET /                      no-op body, same as ?hours=...
  GET /?hours=2              normal mode, look back N hours (default 2)
  GET /?backfill=true&days=N backfill mode, last N days (default 90)
  GET /?account=<key>        restrict to a single account (email domain,
                             name, or id) - useful for debugging

Account -> doc routing comes from the GCS mapping blob (config-sync is
the writer). The mapping value's `docId` field is parsed via
`shared.sheets.parse_id_list` so an operator can grow a customer's
list when a doc hits the cap, e.g. `doc-old,doc-new`. New content
always lands on the LAST id in that list.

Dedup is content-based and operates over the concatenated text of all
docs in the customer's list (not just the tail) so a call already
appended to `doc-old` is never re-appended to `doc-new`.

Cap-hit flow:

  * Each `append_to_doc` call passes the customer's current plaintext
    byte count (cached from the dedup read) so commit 7's cap check
    can refuse the append before any API call.
  * When refused, we serialize the formatted call block to GCS via
    `shared.pending` (partition = email domain) and emit one
    `send_doc_full_alert` per customer per run via `shared.alerts`.
  * The next run starts by draining `pending-calls/<domain>/` for
    every partition, appending each buffered block to whatever doc
    list the operator has by then extended.
"""
import os
import re
from datetime import datetime, timedelta, timezone

from gong_api import (
    format_transcript,
    get_account_info_from_call,
    get_call_details,
    get_calls_in_range,
    get_calls_since,
    get_transcript,
)

from shared import alerts, pending
from shared.gcs_mapping import load_mapping
from shared.google_docs import DocFullError, append_to_doc, get_doc_text
from shared.sheets import (
    batch_update_values,
    get_column_letter,
    parse_id_list,
    read_tab,
)

MAPPING_BLOB = 'account-mapping.json'

# Onboarding sheet: same sheet config-sync reads to build the
# account-mapping.json blob. We write the first/last-call-recorded
# columns at the end of every sync.
SHEET_ID = '1p8CZ5RBGkFSf6aPnUIz8DXai9_UgNZhj7g1JtbPMvzI'
GONG_TAB = 'gong'
FIRST_CALL_COLUMN = 'first-call-recorded'
LAST_CALL_COLUMN = 'last-call-recorded'

# Anchored "GONG CALL" header followed by a Date: line. format_call_for_doc
# is the only writer of this exact three-line block; transcripts that
# happen to mention "GONG CALL: ..." don't carry the surrounding `=====`
# separator so they can't satisfy the regex.
_DATE_RE = re.compile(
    r"\n=+\nGONG CALL:[^\n]*\n=+\nDate: ([^\n]+)",
)


def get_account_mapping():
    return load_mapping(MAPPING_BLOB)


def find_mapping_for_account(account_id, account_name):
    """Return (domain_key, mapping_value) for an account, or (None, None) if unmapped.

    Matches account_id first, then exact account_name, then case-
    insensitive account_name. Returning the matched key (which is the
    customer email-domain in the production mapping) lets the caller
    use it as the GCS pending-queue partition without re-querying.
    """
    account_mapping = get_account_mapping()

    if account_id and account_id in account_mapping:
        return account_id, account_mapping[account_id]
    if account_name and account_name in account_mapping:
        return account_name, account_mapping[account_name]
    if account_name:
        account_lower = account_name.lower()
        for key, value in account_mapping.items():
            if key.lower() == account_lower:
                return key, value
    return None, None


def format_call_for_doc(call_details, transcript, summary):
    """Render a Gong call into the doc-ready text block we append.

    The "GONG CALL: <title>" header + formatted "Date:" line is what
    our content-based dedup AND the first/last-call-recorded date
    extraction key off of, so don't change the format without updating
    both `process_calls` (dedup) and `_DATE_RE` (extraction).
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


def _seed_customer_cache(domain_key, doc_ids, customer_text_cache):
    """Populate `customer_text_cache[domain_key]` with concatenated text from all docs.

    No-op if the cache already has an entry for the customer (drain
    populated it). On read failure we cache an empty string and log;
    this matches the historical "best-effort dedup" behaviour - we'd
    rather double-append a call once than skip everything because one
    doc was unreadable.
    """
    if domain_key in customer_text_cache:
        return
    parts = []
    for doc_id in doc_ids:
        try:
            parts.append(get_doc_text(doc_id))
        except Exception as e:
            print(f"Could not read doc {doc_id} for dedup, proceeding without: {e}")
            parts.append("")
    customer_text_cache[domain_key] = "".join(parts)


def _drain_pending(customer_text_cache, alerted_customers):
    """Drain `pending-calls/<domain>/` for every partition before normal sync.

    For each pending block we read fresh doc text once (seeding the
    customer_text_cache so process_calls reuses it), then attempt the
    append. On DocFullError we alert once and stop draining for that
    customer - subsequent items would just hit the same wall.

    Returns the number of items successfully drained.
    """
    drained = 0
    try:
        partitions = pending.list_partitions(pending.PREFIX_GONG)
    except Exception as e:
        print(f"Could not list pending-calls partitions: {e}")
        return drained

    if not partitions:
        return drained

    print(f"Draining pending calls for {len(partitions)} partition(s)")
    mapping = get_account_mapping()

    for domain_key in sorted(partitions):
        entry = mapping.get(domain_key)
        if not entry:
            print(f"Pending partition '{domain_key}' has no current mapping, skipping drain")
            continue
        doc_ids = parse_id_list(entry.get('docId', ''))
        if not doc_ids:
            print(f"Pending partition '{domain_key}' has empty docId, skipping drain")
            continue

        _seed_customer_cache(domain_key, doc_ids, customer_text_cache)

        for key, payload in pending.drain(pending.PREFIX_GONG, domain_key):
            content = payload.get('content', '')
            if not content:
                pending.delete(pending.PREFIX_GONG, domain_key, key)
                continue
            try:
                target_doc_id = append_to_doc(
                    doc_ids, content,
                    current_text_bytes=len(customer_text_cache[domain_key].encode('utf-8')),
                )
                customer_text_cache[domain_key] += content
                pending.delete(pending.PREFIX_GONG, domain_key, key)
                drained += 1
                print(f"Drained pending call {payload.get('id')} -> {target_doc_id}")
            except DocFullError as e:
                # Doc list is still capped. Alert once and stop
                # draining this customer; remaining items stay in GCS
                # for the next run after the operator extends the list.
                print(f"Drain hit cap on {domain_key} (doc {e.doc_id}, {e.current_bytes} bytes)")
                pending_count = pending.count(pending.PREFIX_GONG, domain_key)
                alerts.send_doc_full_alert(
                    customer_label=entry.get('customerName') or domain_key,
                    customer_key=domain_key,
                    doc_ids=doc_ids,
                    pending_count=pending_count,
                    service='gong',
                    alerted_customers=alerted_customers,
                )
                break
            except Exception as e:
                print(f"Drain failed for pending {key}: {e}")
                # Leave the blob in GCS, try again next run.
                continue

    if drained:
        print(f"Drained {drained} pending call(s)")
    return drained


def process_calls(calls, account_filter=None, customer_text_cache=None, alerted_customers=None):
    """Fetch details + transcripts for `calls` and append each to its doc list.

    Dedup is content-based against the concatenated text of all docs
    in the customer's list. The cache is keyed by `domain_key` (the
    matched account-mapping key) so multiple docs share one entry.

    On `DocFullError`, the formatted call block is enqueued to
    `shared.pending` under partition=domain_key and a one-per-run
    alert is fired via `shared.alerts.send_doc_full_alert`. Returns
    `(processed_count, errors, skipped_accounts, skipped_dupes,
    buffered_count)`.

    Caller can pre-seed `customer_text_cache` (we use it for dedup
    and mutate it on success) and `alerted_customers` (which we use
    AND mutate via shared.alerts).
    """
    if customer_text_cache is None:
        customer_text_cache = {}
    if alerted_customers is None:
        alerted_customers = set()

    if not calls:
        return (0, [], {}, 0, 0)

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
    buffered_count = 0
    errors = []
    skipped_accounts = {}

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

            domain_key, mapping = find_mapping_for_account(account_id, account_name)
            if not mapping:
                key = account_name or account_id
                skipped_accounts[key] = skipped_accounts.get(key, 0) + 1
                print(f"No mapping found for account '{account_name}' (ID: {account_id}), skipping")
                continue

            doc_ids = parse_id_list(mapping.get("docId", ""))
            if not doc_ids:
                print(f"Mapping for '{domain_key}' has empty docId, skipping call {call_id}")
                continue

            _seed_customer_cache(domain_key, doc_ids, customer_text_cache)
            existing_text = customer_text_cache[domain_key]

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
            if dedup_key in existing_text and dedup_date and dedup_date in existing_text:
                print(f"Skipping duplicate call '{call_title}' (already in {domain_key}'s docs)")
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

            try:
                append_to_doc(
                    doc_ids, doc_content,
                    current_text_bytes=len(existing_text.encode('utf-8')),
                )
            except DocFullError as e:
                print(f"Doc full for {domain_key} (doc {e.doc_id}); buffering call '{call_title}'")
                try:
                    pending.enqueue(
                        pending.PREFIX_GONG, domain_key, doc_content,
                        meta={'call_id': call_id, 'title': call_title},
                        unique_id=call_id,
                    )
                    buffered_count += 1
                except Exception as enqueue_err:
                    err_msg = f"Failed to buffer call {call_id}: {enqueue_err}"
                    print(err_msg)
                    errors.append(err_msg)
                    continue
                pending_count = pending.count(pending.PREFIX_GONG, domain_key)
                alerts.send_doc_full_alert(
                    customer_label=mapping.get('customerName') or domain_key,
                    customer_key=domain_key,
                    doc_ids=doc_ids,
                    pending_count=pending_count,
                    service='gong',
                    alerted_customers=alerted_customers,
                )
                continue

            customer_text_cache[domain_key] = existing_text + doc_content
            print(f"Successfully synced call '{call_title}' for '{account_name}'")
            processed_count += 1

        except Exception as e:
            error_msg = f"Error processing call {call_id}: {e}"
            print(error_msg)
            errors.append(error_msg)

    if skipped_dupes:
        print(f"Skipped {skipped_dupes} duplicate calls already in docs")
    if buffered_count:
        print(f"Buffered {buffered_count} calls to GCS pending-calls/")

    return (processed_count, errors, skipped_accounts, skipped_dupes, buffered_count)


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

    customer_text_cache = {}
    alerted_customers = set()

    drained = _drain_pending(customer_text_cache, alerted_customers)

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

    processed_count, errors, skipped_accounts, skipped_dupes, buffered_count = process_calls(
        calls, account_filter,
        customer_text_cache=customer_text_cache,
        alerted_customers=alerted_customers,
    )

    # Always run date-range writeback - even if we processed zero new
    # calls, drains may have happened, and existing docs accumulated
    # from previous runs still need their first/last-call-recorded
    # cells initialised on first deploy. _write_call_date_ranges
    # reads docs FRESH so buffered (un-appended) calls don't leak
    # into the date columns.
    try:
        _write_call_date_ranges()
    except Exception as e:
        print(f"Error writing call date ranges: {e}")

    result = {
        "message": f"Processed {processed_count} calls",
        "processed": processed_count,
        "drained": drained,
        "buffered": buffered_count,
        "skipped_dupes": skipped_dupes,
        "total_found": len(calls) if calls else 0,
        "skipped_accounts": skipped_accounts or None,
        "errors": errors or None,
    }

    print(f"Gong sync complete: {result}")
    return result, 200


def _parse_call_date(date_str):
    """Parse the human format format_call_for_doc emits, or return None.

    Date strings in the doc look like "July 04, 2025 at 03:30 PM". A
    handful of older blocks may carry the raw ISO string if the
    format_call_for_doc fallback fired; we accept those too.
    """
    for fmt in ("%B %d, %Y at %I:%M %p",):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_call_dates(doc_text):
    """Pull every `Date: ...` line that follows a GONG CALL header.

    Returns a list of `datetime` objects. Unparseable date strings are
    silently dropped so one bad block doesn't poison the whole row.
    """
    dates = []
    for match in _DATE_RE.finditer(doc_text):
        dt = _parse_call_date(match.group(1).strip())
        if dt is not None:
            dates.append(dt)
    return dates


def _write_call_date_ranges():
    """Update first-call-recorded / last-call-recorded for every gong row.

    Reads each row's `document-id` cell (parse_id_list - one or many
    docs), fetches the FRESH plaintext of every doc, runs the anchored
    `_DATE_RE` over the concatenation, and writes the min/max dates
    back as `MM/DD/YYYY`.

    Reads docs fresh (NOT from the in-process customer_text_cache) so
    calls that were buffered to GCS this run - i.e. not actually
    appended to the doc - do NOT show up in the date range. The whole
    point of the cap-hit alert is that the operator can trust this
    column reflects what's in the doc.

    Silent no-op if neither column header is on the sheet, or if no
    rows have any matchable dates. Errors per-row are logged and the
    row is skipped so one unreadable doc doesn't sink the batch.
    """
    rows = read_tab(SHEET_ID, GONG_TAB)
    if not rows:
        return

    headers = [h for h in rows[0].keys() if h != '_row_index']
    first_col = get_column_letter(headers, FIRST_CALL_COLUMN)
    last_col = get_column_letter(headers, LAST_CALL_COLUMN)
    if not first_col and not last_col:
        print(
            f"Sheet has neither '{FIRST_CALL_COLUMN}' nor '{LAST_CALL_COLUMN}' column; "
            "skipping date-range write."
        )
        return

    updates = []
    for row in rows:
        doc_ids = parse_id_list(row.get('document-id', ''))
        if not doc_ids:
            continue

        text_parts = []
        for doc_id in doc_ids:
            try:
                text_parts.append(get_doc_text(doc_id))
            except Exception as e:
                print(f"Skipping doc {doc_id} for date range: {e}")
        if not text_parts:
            continue
        text = "".join(text_parts)
        dates = _extract_call_dates(text)
        if not dates:
            continue

        first_str = min(dates).strftime('%m/%d/%Y')
        last_str = max(dates).strftime('%m/%d/%Y')

        if first_col:
            updates.append((f"{GONG_TAB}!{first_col}{row['_row_index']}", first_str))
        if last_col:
            updates.append((f"{GONG_TAB}!{last_col}{row['_row_index']}", last_str))

    if updates:
        batch_update_values(SHEET_ID, updates)
        print(f"Updated first/last-call-recorded on {len(updates)} cell(s)")


if __name__ == '__main__':
    from flask import Flask, request
    app = Flask(__name__)

    @app.route('/', methods=['GET', 'POST'])
    def handle():
        return gong_sync(request)

    port = int(os.environ.get('PORT', 3001))
    print(f'Server running on port {port}')
    app.run(host='0.0.0.0', port=port)
