from google.oauth2 import service_account
from googleapiclient.discovery import build

# Google Docs API client (initialized lazily)
_docs_client = None


def get_docs_client():
    """Get authenticated Google Docs client"""
    global _docs_client
    
    if _docs_client is not None:
        return _docs_client

    # Uses GOOGLE_APPLICATION_CREDENTIALS environment variable automatically
    # Or you can specify credentials explicitly:
    # credentials = service_account.Credentials.from_service_account_file(
    #     'service-account.json',
    #     scopes=['https://www.googleapis.com/auth/documents']
    # )
    
    _docs_client = build('docs', 'v1')
    return _docs_client


def append_message_to_doc(doc_id, user_name, timestamp, text):
    """Append a formatted message to a Google Doc"""
    docs = get_docs_client()
    
    # Format the message
    formatted_message = f"[{timestamp}] {user_name}:\n{text}\n\n"

    # Get the document to find the end index
    doc = docs.documents().get(documentId=doc_id).execute()
    end_index = doc['body']['content'][-1]['endIndex'] - 1

    # Insert text at the end of the document
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={
            'requests': [
                {
                    'insertText': {
                        'location': {'index': end_index},
                        'text': formatted_message
                    }
                }
            ]
        }
    ).execute()

    return True


def initialize_doc(doc_id, customer_name):
    """
    Create initial header in a new Google Doc.
    Call this once when setting up a new customer doc.
    """
    docs = get_docs_client()
    
    header = f"""===========================================
SLACK CONVERSATION LOG: {customer_name}
===========================================
This document is automatically updated with Slack messages.
Connected to NotebookLM for AI-powered context.
-------------------------------------------

"""

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={
            'requests': [
                {
                    'insertText': {
                        'location': {'index': 1},
                        'text': header
                    }
                }
            ]
        }
    ).execute()

    return True
