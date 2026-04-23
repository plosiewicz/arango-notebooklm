"""
Gong to Google Docs sync Cloud Function.
Triggered daily by Cloud Scheduler to sync recent call transcripts.
Supports backfill mode for historical calls.
"""
import json
import os
import time as _time
from datetime import datetime, timedelta

from google.cloud import storage
from gong_api import (
    get_calls_since,
    get_calls_in_range,
    get_call_details,
    get_transcript,
    format_transcript,
    get_account_info_from_call
)
from google_docs import append_to_doc, format_call_for_doc, get_doc_text


# GCS config for account mapping
GCS_BUCKET = os.environ.get('CONFIG_BUCKET', 'slack-notebooklm-config')
GCS_MAPPING_BLOB = 'account-mapping.json'

# Mapping cache (refreshed every 5 minutes)
_mapping_cache = None
_mapping_loaded_at = 0
CACHE_TTL = 300  # 5 minutes


def get_account_mapping():
    """Load account mapping from GCS with 5-minute cache."""
    global _mapping_cache, _mapping_loaded_at

    if _mapping_cache and (_time.time() - _mapping_loaded_at < CACHE_TTL):
        return _mapping_cache

    try:
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(GCS_MAPPING_BLOB)
        _mapping_cache = json.loads(blob.download_as_text())
        _mapping_loaded_at = _time.time()
        print(f"Loaded account mapping from GCS ({len(_mapping_cache)} accounts)")
        return _mapping_cache
    except Exception as e:
        print(f"Error loading account mapping from GCS: {e}")
        # Fall back to local file if GCS fails
        if _mapping_cache:
            print("Using stale cache")
            return _mapping_cache
        try:
            with open('account-mapping.json', 'r') as f:
                _mapping_cache = json.load(f)
                _mapping_loaded_at = _time.time()
                print("Fell back to local account-mapping.json")
                return _mapping_cache
        except Exception:
            return {}

# Track processed calls to avoid duplicates (in production, use a database)
processed_calls_file = '/tmp/processed_gong_calls.json'


def load_processed_calls():
    """Load set of already processed call IDs."""
    try:
        with open(processed_calls_file, 'r') as f:
            return set(json.load(f))
    except:
        return set()


def save_processed_calls(call_ids):
    """Save processed call IDs."""
    try:
        with open(processed_calls_file, 'w') as f:
            json.dump(list(call_ids), f)
    except Exception as e:
        print(f"Warning: Could not save processed calls: {e}")


def find_mapping_for_account(account_id, account_name):
    """
    Find the mapping for an account by ID or name.
    Tries ID first (more reliable), then falls back to name.
    """
    account_mapping = get_account_mapping()

    # Try exact match on account ID
    if account_id and account_id in account_mapping:
        return account_mapping[account_id]
    
    # Try exact match on account name
    if account_name and account_name in account_mapping:
        return account_mapping[account_name]
    
    # Try case-insensitive match on account name
    if account_name:
        for key, value in account_mapping.items():
            if key.lower() == account_name.lower():
                return value
    
    return None


def process_calls(calls, skip_processed=True, account_filter=None):
    """
    Process a list of calls - fetch details, transcripts, and sync to docs.
    
    Args:
        calls: List of call objects from Gong API
        skip_processed: Whether to skip already processed calls
        account_filter: Optional account key (email domain, name, or ID) to process only
    
    Returns:
        Tuple of (processed_count, errors_list, skipped_accounts)
    """
    if not calls:
        return (0, [], {})
    
    # Load previously processed calls
    processed_calls = load_processed_calls() if skip_processed else set()
    
    # Filter out already processed calls if needed
    if skip_processed:
        new_calls = [c for c in calls if c.get("id") not in processed_calls]
        print(f"Found {len(calls)} calls, {len(new_calls)} are new")
    else:
        new_calls = calls
        print(f"Processing {len(new_calls)} calls (backfill mode)")
    
    if not new_calls:
        return (0, [], {})
    
    # Get detailed info for calls (batch in groups of 100)
    all_detailed = []
    call_ids = [c.get("id") for c in new_calls]
    
    print(f"Fetching details for {len(call_ids)} calls...")
    for i in range(0, len(call_ids), 100):
        batch = call_ids[i:i+100]
        print(f"Fetching batch {i//100 + 1}: {len(batch)} calls")
        detailed = get_call_details(batch)
        print(f"Got {len(detailed)} detailed results")
        all_detailed.extend(detailed)
    
    print(f"Total detailed calls retrieved: {len(all_detailed)}")
    
    # Create a lookup by call ID
    call_details_map = {c.get("metaData", {}).get("id"): c for c in all_detailed}
    print(f"Call details map has {len(call_details_map)} entries")
    
    # Debug: Print first 3 raw calls from the API
    print(f"DEBUG: First 3 raw calls from list API:")
    for i, c in enumerate(new_calls[:3]):
        print(f"  DEBUG Raw call {i}: {c}")
    
    processed_count = 0
    skipped_dupes = 0
    errors = []
    skipped_accounts = {}  # Track accounts with no mapping
    doc_text_cache = {}  # Cache of existing doc text per doc_id for dedup
    
    # Debug: Log first 3 call details to understand data structure
    debug_count = 0
    for call in new_calls:
        call_id = call.get("id")
        
        try:
            # Get detailed call info
            details = call_details_map.get(call_id, {})
            
            # Debug logging for first few calls
            if debug_count < 3:
                print(f"DEBUG call {call_id} basic info: {call}")
                print(f"DEBUG call {call_id} details keys: {list(details.keys()) if details else 'NO DETAILS'}")
                if details:
                    print(f"DEBUG call {call_id} context: {details.get('context', 'NO CONTEXT')}")
                    print(f"DEBUG call {call_id} parties: {details.get('parties', 'NO PARTIES')[:2] if details.get('parties') else 'NO PARTIES'}")
                debug_count += 1
            
            # Get account ID and name
            account_id, account_name = get_account_info_from_call(details)
            
            if not account_id and not account_name:
                print(f"Could not determine account for call {call_id}, skipping")
                continue

            # If filtering by account, skip non-matching calls silently
            if account_filter:
                filter_lower = account_filter.lower()
                match = (
                    (account_id and account_id.lower() == filter_lower) or
                    (account_name and account_name.lower() == filter_lower)
                )
                if not match:
                    continue
            
            # Look up the Google Doc for this account
            mapping = find_mapping_for_account(account_id, account_name)
            
            if not mapping:
                # Track skipped accounts for reporting
                key = account_name or account_id
                skipped_accounts[key] = skipped_accounts.get(key, 0) + 1
                print(f"No mapping found for account '{account_name}' (ID: {account_id}), skipping")
                continue
            
            doc_id = mapping.get("docId")

            # Load existing doc text for dedup (cached per doc)
            if doc_id not in doc_text_cache:
                try:
                    doc_text_cache[doc_id] = get_doc_text(doc_id)
                    print(f"Cached doc text for {doc_id} ({len(doc_text_cache[doc_id])} chars)")
                except Exception as e:
                    print(f"Could not read doc {doc_id} for dedup, proceeding without: {e}")
                    doc_text_cache[doc_id] = ""

            # Build dedup key from call title + date
            call_title = details.get("metaData", {}).get("title", "Untitled Call")
            call_started = details.get("metaData", {}).get("started", "")
            if call_started:
                try:
                    from datetime import datetime as _dt
                    _d = _dt.fromisoformat(call_started.replace("Z", "+00:00"))
                    dedup_date = _d.strftime("%B %d, %Y at %I:%M %p")
                except Exception:
                    dedup_date = call_started
            else:
                dedup_date = ""

            dedup_key = f"GONG CALL: {call_title}"
            if dedup_key in doc_text_cache[doc_id] and dedup_date and dedup_date in doc_text_cache[doc_id]:
                print(f"Skipping duplicate call '{call_title}' already in doc {doc_id}")
                skipped_dupes += 1
                continue

            # Get transcript
            transcript_entries = get_transcript(call_id)
            
            # Build participant lookup for transcript formatting
            parties = details.get("parties", [])
            participants = {p.get("speakerId"): p for p in parties}
            
            # Format transcript
            formatted_transcript = format_transcript(transcript_entries, participants)
            
            # Get summary/brief
            content = details.get("content", {})
            summary = content.get("brief")
            
            # Build call details for doc formatting
            call_info = {
                "title": details.get("metaData", {}).get("title", "Untitled Call"),
                "started": details.get("metaData", {}).get("started"),
                "duration": details.get("metaData", {}).get("duration", 0),
                "parties": parties
            }
            
            # Format for doc
            doc_content = format_call_for_doc(call_info, formatted_transcript, summary)
            
            # Append to Google Doc
            append_to_doc(doc_id, doc_content)

            # Update cached doc text so later calls in this run also dedup
            doc_text_cache[doc_id] += doc_content
            
            print(f"Successfully synced call '{call_info['title']}' for '{account_name}'")
            processed_calls.add(call_id)
            processed_count += 1
            
        except Exception as e:
            error_msg = f"Error processing call {call_id}: {str(e)}"
            print(error_msg)
            errors.append(error_msg)
    
    # Save processed calls
    save_processed_calls(processed_calls)
    
    if skipped_dupes:
        print(f"Skipped {skipped_dupes} duplicate calls already in docs")

    return (processed_count, errors, skipped_accounts, skipped_dupes)


def gong_sync(request):
    """
    Main Cloud Function entry point.
    
    Query parameters:
    - backfill: Set to 'true' to enable backfill mode
    - days: Number of days to backfill (default: 90)
    - hours: For regular sync, hours to look back (default: 25)
    - account: Optional account filter (email domain, name, or ID) to process only one account
    """
    # Parse query parameters
    args = request.args if hasattr(request, 'args') else {}
    
    backfill_mode = args.get('backfill', 'false').lower() == 'true'
    account_filter = args.get('account', '').strip() or None
    
    print(f"Starting Gong sync at {datetime.utcnow().isoformat()}")
    print(f"Backfill mode: {backfill_mode}")
    if account_filter:
        print(f"Account filter: {account_filter}")
    
    if backfill_mode:
        # Backfill mode - get historical calls
        days = int(args.get('days', 90))
        print(f"Backfill: Fetching calls from the last {days} days")
        
        to_date = datetime.utcnow()
        from_date = to_date - timedelta(days=days)
        
        calls = get_calls_in_range(from_date, to_date)
        skip_processed = False  # Process all calls in backfill mode
    else:
        # Normal mode - get recent calls
        hours = int(args.get('hours', 25))
        print(f"Normal mode: Fetching calls from the last {hours} hours")
        
        calls = get_calls_since(hours_ago=hours)
        skip_processed = True
    
    if not calls:
        print("No calls found in the specified time range.")
        return {"message": "No calls to process", "processed": 0}, 200
    
    print(f"Found {len(calls)} calls to process")
    
    # Process the calls
    processed_count, errors, skipped_accounts, skipped_dupes = process_calls(calls, skip_processed, account_filter)
    
    result = {
        "message": f"Processed {processed_count} calls",
        "processed": processed_count,
        "skipped_dupes": skipped_dupes,
        "total_found": len(calls),
        "skipped_accounts": skipped_accounts if skipped_accounts else None,
        "errors": errors if errors else None
    }
    
    print(f"Gong sync complete: {result}")
    return result, 200


# For local testing
if __name__ == '__main__':
    from flask import Flask, request
    app = Flask(__name__)

    @app.route('/', methods=['GET', 'POST'])
    def handle():
        return gong_sync(request)

    port = int(os.environ.get('PORT', 3001))
    print(f'Server running on port {port}')
    app.run(host='0.0.0.0', port=port)
