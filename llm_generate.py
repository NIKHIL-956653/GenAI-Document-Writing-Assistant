import requests
import os
from dotenv import load_dotenv

from retry import request_with_retry

# Populated after each OpenRouter call so main.py can report real token
# counts and cost instead of guesses. Reset per call.
LAST_USAGE = {}

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
    global LAST_USAGE
    LAST_USAGE = {}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"model": OPENROUTER_MODEL, "messages": build_messages(ticket)}

    response = request_with_retry(
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=120,
        label=f"openrouter {OPENROUTER_MODEL}",
    )

    if response.status_code != 200:
        print(f"Error: {response.status_code} — {response.text}")
        return ""

    data = response.json()
    LAST_USAGE = {
        "model": OPENROUTER_MODEL,
        "prompt_tokens": data.get("usage", {}).get("prompt_tokens"),
        "completion_tokens": data.get("usage", {}).get("completion_tokens"),
        "total_tokens": data.get("usage", {}).get("total_tokens"),
    }
    return data["choices"][0]["message"]["content"]


def generate_document_ollama(ticket: dict) -> str:
    """Local, free, private — same prompt, different transport. Slow on CPU."""
    global LAST_USAGE
    LAST_USAGE = {}

    payload = {"model": OLLAMA_MODEL, "messages": build_messages(ticket), "stream": False}

    response = request_with_retry(
        "POST",
        "http://localhost:11434/api/chat",
        json=payload,
        timeout=600,
        label=f"ollama {OLLAMA_MODEL}",
    )

    if response.status_code != 200:
        print(f"Error: {response.status_code} — {response.text}")
        return ""

    data = response.json()
    LAST_USAGE = {
        "model": OLLAMA_MODEL,
        "prompt_tokens": data.get("prompt_eval_count"),
        "completion_tokens": data.get("eval_count"),
        "total_tokens": (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0),
    }
    return data["message"]["content"]


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