"""Google Docs helpers shared by slack-sync and gong-sync.

The services only ever need three things from the Docs API: a client,
the raw text of a doc (for dedup), and an append to the end of a doc.
Everything service-specific (message / call formatting) stays in the
caller.
"""
from googleapiclient.discovery import build

_docs_client = None


def get_docs_client():
    """Return a cached, authenticated Google Docs API client.

    Uses Application Default Credentials, so inside Cloud Functions this
    picks up the function's runtime service account automatically.
    """
    global _docs_client
    if _docs_client is None:
        _docs_client = build('docs', 'v1')
    return _docs_client


def get_doc_text(doc_id):
    """Return the full plain text of a Google Doc.

    Used by both services for content-based dedup: we read the doc once
    per run and skip any message/call whose header already appears.
    """
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
    """Append `content` to the end of a Google Doc.

    Finds the current end-of-body index and issues a single insertText
    batchUpdate. Callers are responsible for any formatting/newlines.
    """
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
                        'text': content,
                    }
                }
            ]
        },
    ).execute()

    return True
