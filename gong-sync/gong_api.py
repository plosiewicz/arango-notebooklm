"""Gong API client for fetching calls, transcripts, and summaries."""
import base64
from datetime import datetime, timedelta

import requests

from shared.secrets import get_secret


GONG_API_BASE = "https://api.gong.io/v2"

_encoded_creds = None


def get_encoded_credentials():
    """Return the base64-encoded Gong API credentials for Basic auth.

    The secret is stored as `accessKeyId:accessKeySecret` and we encode
    it ourselves. If a future secret happens to already be base64 (no
    colon), we pass it through.
    """
    global _encoded_creds
    if _encoded_creds is not None:
        return _encoded_creds

    raw_creds = get_secret('gong-api-key')
    if ':' in raw_creds and not raw_creds.startswith('eyJ'):
        _encoded_creds = base64.b64encode(raw_creds.encode()).decode()
    else:
        _encoded_creds = raw_creds
    return _encoded_creds


def get_headers():
    return {
        "Authorization": f"Basic {get_encoded_credentials()}",
        "Content-Type": "application/json",
    }


def get_calls_since(hours_ago=24):
    """
    Get all calls from the last N hours.
    Returns list of call objects with basic info.
    """
    # Calculate time range
    to_time = datetime.utcnow()
    from_time = to_time - timedelta(hours=hours_ago)
    
    url = f"{GONG_API_BASE}/calls"
    params = {
        "fromDateTime": from_time.isoformat() + "Z",
        "toDateTime": to_time.isoformat() + "Z"
    }
    
    response = requests.get(url, headers=get_headers(), params=params)
    
    if response.status_code != 200:
        print(f"Error fetching calls: {response.status_code} - {response.text}")
        return []
    
    data = response.json()
    return data.get("calls", [])


def get_calls_in_range(from_date, to_date):
    """
    Get all calls in a specific date range.
    Handles pagination for large result sets.
    
    Args:
        from_date: datetime object for start of range
        to_date: datetime object for end of range
    
    Returns:
        List of call objects
    """
    all_calls = []
    cursor = None
    
    while True:
        url = f"{GONG_API_BASE}/calls"
        params = {
            "fromDateTime": from_date.isoformat() + "Z",
            "toDateTime": to_date.isoformat() + "Z"
        }
        if cursor:
            params["cursor"] = cursor
        
        response = requests.get(url, headers=get_headers(), params=params)
        
        if response.status_code != 200:
            print(f"Error fetching calls: {response.status_code} - {response.text}")
            break
        
        data = response.json()
        calls = data.get("calls", [])
        all_calls.extend(calls)
        
        # Check for more pages
        cursor = data.get("records", {}).get("cursor")
        if not cursor:
            break
        
        print(f"Fetched {len(all_calls)} calls so far, getting more...")
    
    return all_calls


def get_call_details(call_ids):
    """
    Get detailed info for specific calls including participants and accounts.
    """
    if not call_ids:
        return []
    
    url = f"{GONG_API_BASE}/calls/extensive"
    payload = {
        "filter": {
            "callIds": call_ids
        },
        "contentSelector": {
            "exposedFields": {
                "parties": True,
                "content": {
                    "brief": True
                }
            }
        }
    }
    
    response = requests.post(url, headers=get_headers(), json=payload)
    
    if response.status_code != 200:
        print(f"Error fetching call details: {response.status_code} - {response.text}")
        return []
    
    data = response.json()
    return data.get("calls", [])


def get_transcript(call_id):
    """
    Get the full transcript for a specific call.
    Returns list of transcript entries with speaker and text.
    """
    url = f"{GONG_API_BASE}/calls/transcript"
    payload = {
        "filter": {
            "callIds": [call_id]
        }
    }
    
    response = requests.post(url, headers=get_headers(), json=payload)
    
    if response.status_code != 200:
        print(f"Error fetching transcript for {call_id}: {response.status_code} - {response.text}")
        return None
    
    data = response.json()
    # Response contains callTranscripts array
    transcripts = data.get("callTranscripts", [])
    if transcripts:
        return transcripts[0].get("transcript", [])
    return []


def format_transcript(transcript_entries, participants):
    """
    Format transcript entries into readable text.
    
    Args:
        transcript_entries: List of transcript segments
        participants: Dict mapping speakerId to participant info
    
    Returns:
        Formatted transcript string
    """
    if not transcript_entries:
        return "No transcript available."
    
    lines = []
    for entry in transcript_entries:
        speaker_id = entry.get("speakerId")
        speaker_name = participants.get(speaker_id, {}).get("name", "Unknown")
        text = entry.get("sentences", [])
        
        # Combine sentences
        full_text = " ".join([s.get("text", "") for s in text])
        
        # Format timestamp (convert ms to mm:ss)
        start_ms = entry.get("start", 0)
        minutes = int(start_ms // 60000)
        seconds = int((start_ms % 60000) // 1000)
        timestamp = f"[{minutes:02d}:{seconds:02d}]"
        
        lines.append(f"{timestamp} {speaker_name}: {full_text}")
    
    return "\n\n".join(lines)


def get_account_info_from_call(call_details):
    """
    Extract account ID and name from call details.
    Returns tuple of (account_id, account_name) or (None, None) if not found.
    
    Checks multiple sources:
    1. CRM context (Salesforce/HubSpot account ID)
    2. External participant company info
    """
    # First, try to get from CRM context (most reliable)
    context = call_details.get("context", [])
    for ctx in context:
        objects = ctx.get("objects", [])
        for obj in objects:
            if obj.get("objectType") == "Account":
                account_id = obj.get("objectId")
                account_name = obj.get("name")
                if account_id:
                    return (account_id, account_name)
    
    # Fallback: Get from external participants
    parties = call_details.get("parties", [])
    for party in parties:
        if party.get("affiliation") == "External":
            company = party.get("company")
            if company:
                return (company, company)
            
            # Fall back to email domain
            email = party.get("emailAddress", "")
            if email and "@" in email:
                domain = email.split("@")[1].lower()
                # Skip generic email domains
                if domain not in ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"]:
                    return (domain, domain)
    
    return (None, None)


