"""Google Docs helpers shared by slack-sync and gong-sync.

The services only ever need three things from the Docs API: a client,
the raw text of a doc (for dedup), and an append to the end of a doc.
Everything service-specific (message / call formatting) stays in the
caller.

Cap awareness:

The Google Docs API rejects documents larger than 10MB. We use
`DOC_CAP_BYTES` (6MB plaintext) as a conservative proxy: real doc
storage size includes formatting/markup overhead, so we leave a
generous headroom rather than hit the wall mid-batch. When the cap
is enforced (see commit 7) `append_to_doc` will raise `DocFullError`
*before* issuing the batchUpdate, and callers buffer the content to
GCS via `shared.pending`.
"""
from googleapiclient.discovery import build

# 6 MB plaintext threshold. The hard wall is 10 MB on Google's side
# but that includes formatting overhead we can't measure cheaply, so
# we cap on plaintext and leave headroom.
DOC_CAP_BYTES = 6 * 1024 * 1024


class DocFullError(Exception):
    """Raised by append_to_doc when the target doc is at or above DOC_CAP_BYTES.

    Callers buffer the formatted content to `shared.pending` and emit
    a `send_doc_full_alert` (at most once per customer per run) so an
    operator can extend the doc list. The pending buffer drains on
    the next run that sees an enlarged doc list.

    Attributes:
      doc_id:        the offending doc id (the LAST id in the doc list)
      current_bytes: measured plaintext size at decision time
    """
    def __init__(self, doc_id, current_bytes):
        super().__init__(f"Doc {doc_id} at {current_bytes} bytes (>= {DOC_CAP_BYTES})")
        self.doc_id = doc_id
        self.current_bytes = current_bytes


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


def append_to_doc(doc_id_or_ids, content, current_text_bytes=None):
    """Append `content` to the tail Google Doc and return the doc id used.

    Accepts either a single doc id (legacy callers) or a list of ids
    (multi-doc cap-hit flow); the append always lands on the LAST id
    in the list. Returns the doc id that was actually written to so
    the caller can update its dedup cache.

    `current_text_bytes` is reserved for cap enforcement (commit 7)
    and ignored in this commit. Once enforced, passing a value at or
    above DOC_CAP_BYTES will raise `DocFullError` *before* any
    Google Docs API call so the caller can buffer the content to
    `shared.pending` instead.

    Callers that pass `current_text_bytes=None` opt out of cap
    enforcement (e.g. drain workers that have already fetched fresh
    text and want to append unconditionally).
    """
    if isinstance(doc_id_or_ids, str):
        doc_ids = [doc_id_or_ids]
    else:
        doc_ids = list(doc_id_or_ids)
    if not doc_ids:
        raise ValueError("append_to_doc requires at least one doc id")
    target_doc_id = doc_ids[-1]

    docs = get_docs_client()
    doc = docs.documents().get(documentId=target_doc_id).execute()
    end_index = doc['body']['content'][-1]['endIndex'] - 1

    docs.documents().batchUpdate(
        documentId=target_doc_id,
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

    return target_doc_id
