import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import os

load_dotenv()

JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL  = os.getenv("JIRA_EMAIL")
JIRA_TOKEN  = os.getenv("JIRA_TOKEN")

def publish_to_confluence(title: str, content: str, space_key: str = "KAN") -> str:
    url = f"https://{JIRA_DOMAIN}/wiki/rest/api/content"
    
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {
            "storage": {
                "value": f"<p>{content}</p>",
                "representation": "storage"
            }
        }
    }
    
    response = requests.post(
        url,
        json=payload,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
        headers={"Content-Type": "application/json"}
    )
    
    if response.status_code != 200:
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