import requests
import os
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-lite-001")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:latest")

SYSTEM_PROMPT = """You are a senior technical writer at a software company.
Your job is to write clear, structured product documentation based on Jira tickets.
Always structure your document with these sections:
1. Overview
2. Technical Details
3. Implementation Steps
4. Acceptance Criteria
5. Known Limitations
Only state facts that are in the ticket or are standard engineering practice; if the ticket
does not specify something, say so under Known Limitations instead of inventing it."""


def build_messages(ticket: dict) -> list:
    """One prompt for every backend — the model is swappable, the contract is not."""
    user_prompt = f"""Write a complete technical document for this Jira ticket:

TICKET ID: {ticket['id']}
TITLE: {ticket['title']}
DESCRIPTION: {ticket['description']}
STATUS: {ticket['status']}
LABELS: {", ".join(ticket.get('labels') or []) or "none"}

Generate a full, publication-ready technical document in Markdown."""
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}]


def generate_document_openrouter(ticket: dict) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"model": OPENROUTER_MODEL, "messages": build_messages(ticket)}

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=120,
    )

    if response.status_code != 200:
        print(f"Error: {response.status_code} — {response.text}")
        return ""

    return response.json()["choices"][0]["message"]["content"]


def generate_document_ollama(ticket: dict) -> str:
    """Local, free, private — same prompt, different transport. Slow on CPU."""
    payload = {"model": OLLAMA_MODEL, "messages": build_messages(ticket), "stream": False}

    response = requests.post(
        "http://localhost:11434/api/chat",
        json=payload,
        timeout=600,
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