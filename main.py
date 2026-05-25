from jira_fetch import get_jira_ticket
from llm_generate import generate_document_openrouter, generate_document_ollama
import json

def run_pipeline(ticket_id: str, llm: str = "openrouter"):
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

    # Step 4: Print document
    print(f"\n📄 GENERATED DOCUMENT:")
    print("="*60)
    print(document)
    print("="*60)
    print(f"\n✅ Pipeline complete! Document saved as {filename}")

if __name__ == "__main__":
    # Run the full pipeline
    run_pipeline("DEMO-1", llm="openrouter")