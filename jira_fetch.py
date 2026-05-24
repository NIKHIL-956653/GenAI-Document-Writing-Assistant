import requests
from requests.auth import HTTPBasicAuth
import json

# ==============================
# CONFIG — FILL THESE IN
# ==============================
JIRA_DOMAIN = "yourname.atlassian.net"       # e.g. nikhil.atlassian.net
JIRA_EMAIL  = "your@email.com"               # your Atlassian login email
JIRA_TOKEN  = "your_api_token_here"          # from id.atlassian.com
TICKET_ID   = "DOCGEN-1"                     # your first ticket ID

# ==============================
# FETCH TICKET
# ==============================
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

# ==============================
# RUN
# ==============================
if __name__ == "__main__":
    ticket = get_jira_ticket(TICKET_ID)
    print(json.dumps(ticket, indent=2))