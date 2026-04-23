"""
Google Docs API helper for appending transcripts.
"""
from googleapiclient.discovery import build

# Google Docs API client (initialized lazily)
_docs_client = None


def get_docs_client():
    """Get authenticated Google Docs client."""
    global _docs_client
    
    if _docs_client is not None:
        return _docs_client

    _docs_client = build('docs', 'v1')
    return _docs_client


def get_doc_text(doc_id):
    """Read all text content from a Google Doc for deduplication."""
    docs = get_docs_client()
    doc = docs.documents().get(documentId=doc_id).execute()

    text_parts = []
    for element in doc.get('body', {}).get('content', []):
        paragraph = element.get('paragraph')
        if paragraph:
            for run in paragraph.get('elements', []):
                text_run = run.get('textRun')
                if text_run:
                    text_parts.append(text_run.get('content', ''))

    return ''.join(text_parts)


def append_to_doc(doc_id, content):
    """Append content to the end of a Google Doc."""
    docs = get_docs_client()
    
    # Get the document to find the end index
    doc = docs.documents().get(documentId=doc_id).execute()
    end_index = doc['body']['content'][-1]['endIndex'] - 1

    # Insert text at the end
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={
            'requests': [
                {
                    'insertText': {
                        'location': {'index': end_index},
                        'text': content
                    }
                }
            ]
        }
    ).execute()

    return True


def format_call_for_doc(call_details, transcript, summary):
    """
    Format a Gong call into a document-ready string.
    
    Args:
        call_details: Dict with call metadata (title, date, participants, etc.)
        transcript: Formatted transcript string
        summary: AI summary/brief from Gong
    
    Returns:
        Formatted string ready to append to Google Doc
    """
    title = call_details.get("title", "Untitled Call")
    date = call_details.get("started", "Unknown date")
    duration_minutes = call_details.get("duration", 0) // 60
    
    # Get participants
    parties = call_details.get("parties", [])
    participants = []
    for party in parties:
        name = party.get("name", "Unknown")
        company = party.get("company", "")
        if company:
            participants.append(f"{name} ({company})")
        else:
            participants.append(name)
    
    participants_str = ", ".join(participants) if participants else "Unknown"
    
    # Format the date nicely
    if date and date != "Unknown date":
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
            date = dt.strftime("%B %d, %Y at %I:%M %p")
        except:
            pass
    
    # Build the document content
    content = f"""
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
    return content
