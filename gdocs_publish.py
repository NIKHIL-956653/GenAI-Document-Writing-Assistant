import os
import pickle
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive"
]

def get_credentials():
    creds = None
    
    # Load saved token if exists
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)
    
    # If no valid credentials, login
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        
        # Save credentials for next time
        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)
    
    return creds


def create_google_doc(title: str, content: str, public: bool = False) -> str:
    """Create a Doc with the content. Sharing is opt-in — internal docs stay private by default."""
    creds = get_credentials()
    
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    # Create blank doc
    doc = docs_service.documents().create(
        body={"title": title}
    ).execute()

    doc_id = doc["documentId"]
    print(f"Document created with ID: {doc_id}")

    # Insert content
    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {
                    "insertText": {
                        "location": {"index": 1},
                        "text": content
                    }
                }
            ]
        }
    ).execute()

    if public:
        drive_service.permissions().create(
            fileId=doc_id,
            body={"type": "anyone", "role": "reader"}
        ).execute()
        print("Sharing: anyone with the link can view")

    doc_url = f"https://docs.google.com/document/d/{doc_id}"
    print(f"Published! URL: {doc_url}")
    return doc_url


if __name__ == "__main__":
    from jira_fetch import get_jira_ticket
    from llm_generate import generate_document_openrouter

    ticket = get_jira_ticket("DEMO-2")
    print("Ticket fetched!")

    document = generate_document_openrouter(ticket)
    print("Document generated!")

    try:
        url = create_google_doc(
            title=ticket["title"],
            content=document
        )
        print(f"Done! Open this URL: {url}")
    except Exception as e:
        print(f"Error: {e}")