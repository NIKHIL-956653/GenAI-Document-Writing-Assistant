import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import os
import json

from retry import request_with_retry

load_dotenv()

JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL  = os.getenv("JIRA_EMAIL")
JIRA_TOKEN  = os.getenv("JIRA_TOKEN")

def adf_to_text(node) -> str:
    """
    Jira Cloud REST v3 returns rich text as Atlassian Document Format (a JSON tree),
    not a string. Walk it and keep the text so the LLM sees prose, not a Python dict.
    Handles paragraphs, headings, bullet/ordered lists, code blocks, hard breaks.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    children = "".join(adf_to_text(c) for c in node.get("content", []) or [])
    if t in ("paragraph", "heading"):
        return children + "\n"
    if t == "listItem":
        return "- " + children
    if t in ("bulletList", "orderedList", "blockquote"):
        return children + "\n"
    if t == "codeBlock":
        return "```\n" + children + "\n```\n"
    return children


def get_jira_ticket(ticket_id: str) -> dict:
    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{ticket_id}"
    
    response = request_with_retry(
        "GET", url,
        auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN),
        headers={"Accept": "application/json"},
        timeout=30,
        label=f"jira {ticket_id}",
    )
    
    if response.status_code != 200:
        print(f"Error: {response.status_code} — {response.text[:300]}")
        return {}
    
    data = response.json()
    description = adf_to_text(data["fields"].get("description")).strip() or "No description"
    
    ticket = {
        "id":          data["key"],
        "title":       data["fields"]["summary"],
        "description": description,
        "status":      data["fields"]["status"]["name"],
        "labels":      data["fields"].get("labels", []),
    }
    
    return ticket

if __name__ == "__main__":
    ticket = get_jira_ticket("DEMO-1")
    print(json.dumps(ticket, indent=2))