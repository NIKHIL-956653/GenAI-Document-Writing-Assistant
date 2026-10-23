"""
The prompt contract shared by every backend — the model is swappable, the contract is not.

The template supplies the persona, the sections and their format; the
faithfulness rule is appended to every template's system prompt so no
template can drop it.
"""
from __future__ import annotations

from docgen.models import Ticket
from docgen.templates import Template, render_user_prompt

FAITHFULNESS_RULE = (
    "Only state facts that are in the ticket or are standard engineering practice; "
    "if the ticket does not specify something, say so under {unknowns} instead of inventing it."
)


def system_prompt(template: Template) -> str:
    unknowns = template.unknowns_section
    where = f'"{unknowns.title}"' if unknowns else "an explicit note"
    return f"{template.system_prompt.strip()}\n{FAITHFULNESS_RULE.format(unknowns=where)}"


def build_messages(ticket: Ticket, template: Template) -> list[dict]:
    user = render_user_prompt(template, key=ticket.key, title=ticket.title,
                              description=ticket.description, status=ticket.status,
                              labels=ticket.labels)
    return [{"role": "system", "content": system_prompt(template)},
            {"role": "user", "content": user}]
