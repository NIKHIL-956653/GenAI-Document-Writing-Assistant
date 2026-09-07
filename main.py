import argparse

from jira_fetch import get_jira_ticket
from llm_generate import generate_document_openrouter, generate_document_ollama


def run_pipeline(ticket_id: str, llm: str = "openrouter", publish: str = "none", public: bool = False):
    print("\n" + "="*60)
    print(f"🚀 GenAI Document Writing Assistant")
    print("="*60)
    
    # Step 1: Fetch Jira Ticket
    print(f"\n📋 Step 1: Fetching Jira ticket {ticket_id}...")
    ticket = get_jira_ticket(ticket_id)
    if not ticket:
        print("❌ Failed to fetch ticket!")
        return
    print(f"✅ Ticket fetched: {ticket['title']}")

    # Step 2: Generate Document
    print(f"\n🤖 Step 2: Generating document with {llm}...")
    if llm == "openrouter":
        document = generate_document_openrouter(ticket)
    else:
        document = generate_document_ollama(ticket)
    
    if not document:
        print("❌ Failed to generate document!")
        return
    print(f"✅ Document generated! ({len(document)} characters)")

    # Step 3: Save locally
    print(f"\n💾 Step 3: Saving document locally...")
    filename = f"{ticket_id}_document.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# {ticket['title']}\n\n")
        f.write(document)
    print(f"✅ Saved as {filename}")

    # Step 4: Publish (optional)
    if publish == "gdocs":
        from gdocs_publish import create_google_doc
        print(f"\n☁️  Step 4: Publishing to Google Docs...")
        print(f"✅ {create_google_doc(ticket['title'], document, public=public)}")
    elif publish == "confluence":
        from confluence_publish import publish_to_confluence
        print(f"\n☁️  Step 4: Publishing to Confluence...")
        print(f"✅ {publish_to_confluence(ticket['title'], document)}")

    print(f"\n📄 GENERATED DOCUMENT:")
    print("="*60)
    print(document)
    print("="*60)
    print(f"\n✅ Pipeline complete! Document saved as {filename}")
    return filename


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Jira ticket → LLM → technical document")
    ap.add_argument("ticket", nargs="?", default="DEMO-1", help="Jira issue key, e.g. DEMO-2")
    ap.add_argument("--llm", choices=["openrouter", "ollama"], default="openrouter")
    ap.add_argument("--publish", choices=["none", "gdocs", "confluence"], default="none")
    ap.add_argument("--public", action="store_true", help="with --publish gdocs: share as anyone-with-link")
    args = ap.parse_args()
    run_pipeline(args.ticket, llm=args.llm, publish=args.publish, public=args.public)