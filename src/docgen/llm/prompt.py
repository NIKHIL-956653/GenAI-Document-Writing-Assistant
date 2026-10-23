"""The prompt contract shared by every backend — the model is swappable, the contract is not."""
from __future__ import annotations

from docgen.models import Ticket

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


def build_messages(ticket: Ticket) -> list[dict]:
    user_prompt = f"""Write a complete technical document for this Jira ticket:

TICKET ID: {ticket.key}
TITLE: {ticket.title}
DESCRIPTION: {ticket.description}
STATUS: {ticket.status}
LABELS: {", ".join(ticket.labels) or "none"}

Generate a full, publication-ready technical document in Markdown."""
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}]
