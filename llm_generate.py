import requests
import os
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

def generate_document_openrouter(ticket: dict) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    system_prompt = """You are a senior technical writer at a software company.
Your job is to write clear, structured product documentation based on Jira tickets.
Always structure your document with these sections:
1. Overview
2. Technical Details
3. Implementation Steps
4. Acceptance Criteria
5. Known Limitations"""

    user_prompt = f"""
Write a complete technical document for this Jira ticket:

TICKET ID: {ticket['id']}
TITLE: {ticket['title']}
DESCRIPTION: {ticket['description']}
STATUS: {ticket['status']}

Generate a full, publication-ready technical document.
"""

    payload = {
    "model": "google/gemini-2.0-flash-lite-001",
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
}

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload
    )

    if response.status_code != 200:
        print(f"Error: {response.status_code} — {response.text}")
        return ""

    return response.json()["choices"][0]["message"]["content"]


def generate_document_ollama(ticket: dict) -> str:
    system_prompt = """You are a senior technical writer at a software company.
Your job is to write clear, structured product documentation based on Jira tickets.
Always structure your document with these sections:
1. Overview
2. Technical Details
3. Implementation Steps
4. Acceptance Criteria
5. Known Limitations"""

    user_prompt = f"""
Write a complete technical document for this Jira ticket:

TICKET ID: {ticket['id']}
TITLE: {ticket['title']}
DESCRIPTION: {ticket['description']}
STATUS: {ticket['status']}

Generate a full, publication-ready technical document.
"""

    payload = {
        "model": "qwen2.5:latest",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "stream": False
    }

    response = requests.post(
        "http://localhost:11434/api/chat",
        json=payload
    )

    if response.status_code != 200:
        print(f"Error: {response.status_code} — {response.text}")
        return ""

    return response.json()["message"]["content"]


if __name__ == "__main__":
    from jira_fetch import get_jira_ticket

    ticket = get_jira_ticket("DEMO-1")
    print("Ticket fetched!")
    print("Generating document with OpenRouter...")
    
    document = generate_document_openrouter(ticket)
    print("\n" + "="*50)
    print("GENERATED DOCUMENT:")
    print("="*50)
    print(document)