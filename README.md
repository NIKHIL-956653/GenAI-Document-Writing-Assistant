# GenAI Document Writing Assistant

An AI-powered technical documentation assistant that automatically generates 
and publishes product documentation from Jira tickets using LLMs.

## What It Does
Jira Ticket (DEMO-1)
↓
Fetches ticket data via Jira REST API
↓
Feeds ticket into LLM (Gemini / Ollama)
↓
AI generates full technical document
↓
Publishes to Google Docs automatically

## Tech Stack

- **Python** — core language
- **Jira REST API** — ticket data source
- **OpenRouter (Gemini 2.0 Flash)** — cloud LLM
- **Ollama (Qwen)** — local LLM fallback
- **Google Docs API** — document publishing
- **Google Drive API** — file management
- **python-dotenv** — environment management

## Project Structure
GenAI-Document-Writing-Assistant/
├── jira_fetch.py          # Connects to Jira, fetches ticket data
├── llm_generate.py        # Feeds ticket to LLM, generates document
├── confluence_publish.py  # Publishes to Confluence
├── gdocs_publish.py       # Publishes to Google Docs
├── main.py                # Full pipeline runner
├── requirements.txt       # Dependencies
├── .gitignore             # Ignored files
└── .env                   # API keys (not committed)

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/NIKHIL-956653/GenAI-Document-Writing-Assistant.git
cd GenAI-Document-Writing-Assistant
```

### 2. Create virtual environment
```bash
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Create .env file
JIRA_DOMAIN=yourname.atlassian.net
JIRA_EMAIL=your@email.com
JIRA_TOKEN=your_jira_api_token
OPENROUTER_API_KEY=your_openrouter_key

### 5. Add Google credentials
- Go to Google Cloud Console
- Enable Google Docs API and Google Drive API
- Create OAuth 2.0 credentials
- Download as `credentials.json` and place in project root

## Run

### Full pipeline (Jira → LLM → Save locally)
```bash
python main.py
```

### Publish to Google Docs
```bash
python gdocs_publish.py
```

### Fetch Jira ticket only
```bash
python jira_fetch.py
```

## How It Works

### 1. Jira Integration
Connects to Jira REST API using Basic Auth (email + API token).
Fetches ticket fields: title, description, status, labels.

### 2. LLM Document Generation
Builds a dynamic prompt using ticket data and sends it to:
- **OpenRouter** (Gemini 2.0 Flash) — cloud, high quality
- **Ollama** (Qwen) — local, free, private

LLM generates a structured technical document with:
- Overview
- Technical Details
- Implementation Steps
- Acceptance Criteria
- Known Limitations

### 3. Google Docs Publishing
Uses OAuth 2.0 to authenticate with Google.
Creates a new Google Doc and inserts the generated content.
Makes the document publicly viewable.

## Example Output

Input ticket:
DEMO-1: User Authentication Feature
Description: Implement secure login with JWT tokens

Output: Full technical document published to Google Docs with
overview, implementation steps, acceptance criteria, and limitations.

## Results

- ✅ Successfully fetched tickets from Jira REST API
- ✅ Generated technical documents using Gemini 2.0 Flash
- ✅ Published documents to Google Docs automatically
- ✅ Supports both cloud (OpenRouter) and local (Ollama) LLMs

## Author

**Nikhil Chandra Sairam Tokala**  
AI/ML Engineer | GenAI Engineer | DevOps  
Dubai, UAE  
[LinkedIn](https://linkedin.com/in/nikhil-chandra-133ncsr200233) | 
[GitHub](https://github.com/NIKHIL-956653)