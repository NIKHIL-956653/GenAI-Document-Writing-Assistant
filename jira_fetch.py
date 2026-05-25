import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import os
import json

load_dotenv()

JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL  = os.getenv("JIRA_EMAIL")
JIRA_TOKEN  = os.getenv("JIRA_TOKEN")

def get_jira_ticket(ticket_id: str) -> dict:
    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{ticket_id}"
    
    response = requests.get(
        url,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
        headers={"Accept": "application/json"}
    )
    
    if response.status_code != 200:
        print(f"Error: {response.status_code} — {response.text}")
        return {}
    
    data = response.json()
    
    ticket = {
        "id":          data["key"],
        "title":       data["fields"]["summary"],
        "description": data["fields"].get("description", "No description"),
        "status":      data["fields"]["status"]["name"],
        "labels":      data["fields"].get("labels", []),
    }
    
    return ticket

if __name__ == "__main__":
    ticket = get_jira_ticket("DEMO-1")
    print(json.dumps(ticket, indent=2))