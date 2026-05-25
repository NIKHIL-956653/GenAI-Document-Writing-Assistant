from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
import os

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive"
]

SERVICE_ACCOUNT_FILE = "service_account.json"

def create_google_doc(title: str, content: str) -> str:
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=SCOPES
    )

    drive_service = build("drive", "v3", credentials=creds)
    docs_service = build("docs", "v1", credentials=creds)

    # Create file without specifying parent folder
    file_metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document"
    }

    doc = drive_service.files().create(
        body=file_metadata
    ).execute()

    doc_id = doc["id"]
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

    # Share with your personal Gmail
    drive_service.permissions().create(
        fileId=doc_id,
        body={
            "type": "user",
            "role": "writer",
            "emailAddress": "nikhilvijayapuri@gmail.com"
        }
    ).execute()

    doc_url = f"https://docs.google.com/document/d/{doc_id}"
    print(f"Published! URL: {doc_url}")
    return doc_url


if __name__ == "__main__":
    from jira_fetch import get_jira_ticket
    from llm_generate import generate_document_openrouter

    ticket = get_jira_ticket("DEMO-1")
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
        print(f"Error publishing to Google Docs: {e}")