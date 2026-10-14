import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import os

from markdown_convert import markdown_to_storage
from retry import request_with_retry

load_dotenv()

JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL  = os.getenv("JIRA_EMAIL")
JIRA_TOKEN  = os.getenv("JIRA_TOKEN")

def publish_to_confluence(title: str, content: str, space_key: str = "KAN") -> str:
    """
    Publish to Confluence Cloud. Markdown is converted to storage-format
    XHTML first — the earlier version wrapped the entire markdown document
    in a single <p>, so headings and lists rendered as literal characters.
    """
    url = f"https://{JIRA_DOMAIN}/wiki/rest/api/content"

    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {
            "storage": {
                "value": markdown_to_storage(content),
                "representation": "storage"
            }
        }
    }

    response = request_with_retry(
        "POST", url,
        json=payload,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
        headers={"Content-Type": "application/json"},
        timeout=60,
        label=f"confluence {space_key}",
    )

    if response.status_code not in (200, 201):
        print(f"Error: {response.status_code} — {response.text}")
        return ""

    page_url = f"https://{JIRA_DOMAIN}/wiki{response.json()['_links']['webui']}"
    print(f"Published! URL: {page_url}")
    return page_url

if __name__ == "__main__":
    from jira_fetch import get_jira_ticket
    from llm_generate import generate_document_openrouter

    ticket = get_jira_ticket("DEMO-1")
    print("Ticket fetched!")
    
    document = generate_document_openrouter(ticket)
    print("Document generated!")
    
    url = publish_to_confluence(
        title=ticket["title"],
        content=document
    )
    print(f"Done! Check Confluence: {url}")