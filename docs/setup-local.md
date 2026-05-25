# Local setup — Ollama or Docker Model Runner + DynamoDB Local

Run the full PR review system on your laptop with **no AWS account**.  
LLM is served by Ollama (or Docker Desktop's built-in model runner),  
DynamoDB is a local Docker container.

---

## Prerequisites

| Tool | Minimum version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | 0.5+ | Fast Python package manager — manages the venv and Python version |
| Docker | 24+ | Docker Desktop or Docker Engine |
| Docker Compose | v2 | bundled with Docker Desktop |
| Git | any | — |

Install `uv` if you don't have it:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

---

## Step 1 — Clone and install dependencies

```bash
git clone https://github.com/hkcodebase/snarky-squirrel.git
cd snarky-squirrel
git checkout feature/v2

# Create virtual environment with Python 3.12
uv venv --python 3.12

# Install dependencies
uv pip install -r requirements-v2.txt
```

> All subsequent commands use `uv run python …` so you don't need to activate the venv manually.  
> If you prefer to activate: `source .venv/bin/activate` (macOS/Linux) or `.\.venv\Scripts\Activate.ps1` (Windows).

---

## Step 2 — Configure environment

```bash
cp .env.example .env
```

Open `.env` and choose **one** of the two LLM options below.

### Option A — Ollama

```dotenv
LLM_PROVIDER=ollama
LLM_MODEL=gemma4:4b
OLLAMA_BASE_URL=http://localhost:11434

DYNAMODB_ENDPOINT=http://localhost:8000
DYNAMODB_TABLE=pr-review-local-memory

GITHUB_TOKEN=ghp_your_token_here   # required for live PR reviews; skip for diff-only
WEBHOOK_SECRET=skip
```

### Option B — Docker Model Runner (Docker Desktop 4.40+)

```dotenv
LLM_PROVIDER=docker-model
LLM_MODEL=ai/gemma4
DOCKER_MODEL_ENDPOINT=http://localhost:12434/engines/llama.cpp/v1

DYNAMODB_ENDPOINT=http://localhost:8000
DYNAMODB_TABLE=pr-review-local-memory

GITHUB_TOKEN=ghp_your_token_here
WEBHOOK_SECRET=skip
```

> Docker Model Runner is built into Docker Desktop 4.40+.  
> Enable it: **Settings → Beta Features → Enable Docker Model Runner**

---

## Step 3 — Start infrastructure

### Option A — Ollama

```bash
# Starts DynamoDB Local (port 8000) + Ollama (port 11434)
docker-compose --profile ollama up -d

# Verify
docker-compose ps
```

Containers:
- `pr-review-dynamo` — DynamoDB Local on `:8000`
- `pr-review-dynamo-init` — creates the table then exits
- `pr-review-ollama` — Ollama server on `:11434`

### Option B — Docker Model Runner

Pull the model once (Docker handles caching):

```bash
docker model pull ai/gemma4
```

Start only DynamoDB (Ollama container is not needed):

```bash
docker-compose up -d

# Verify
docker-compose ps
```

---

## Step 4 — Pull model and verify DynamoDB

```bash
# Option A — Ollama
uv run python scripts/setup_local.py --provider ollama

# Option B — Docker Model Runner
uv run python scripts/setup_local.py --provider docker-model --model ai/gemma4
```

Expected output (Ollama):

```
PR Review System — local environment setup
Provider: ollama  Model: gemma4:4b
--------------------------------------------------
[DynamoDB] endpoint=http://localhost:8000  table=pr-review-local-memory
  ✓ DynamoDB Local is ready
  ✓ Table 'pr-review-local-memory' created

[Ollama] base_url=http://localhost:11434  model=gemma4:4b
  ✓ Ollama is ready
  Pulling 'gemma4:4b' ...  (~2.5 GB, cached after first pull)
  ✓ Model 'gemma4:4b' ready
==================================================
  Local environment ready!
```

---

## Step 5 — Run the server

```bash
uv run python api.py
# → http://localhost:8080
```

The server auto-reloads on any `.py` file change (`reload=True`).  
For `templates/index.html` or `styles.css` changes, hard-refresh the browser (`Ctrl+Shift+R`).

Health check:

```bash
curl http://localhost:8080/health
# {"status":"ok","llm_provider":"ollama","llm_model":"gemma4:4b",...}
```

---

## Running a review

### From the web UI

Open `http://localhost:8080`, paste a GitHub PR URL, click **Review PR**.

### From curl

```bash
# Trigger a review
curl -X POST http://localhost:8080/review \
     -H 'Content-Type: application/json' \
     -d '{"pr_url": "https://github.com/owner/repo/pull/42"}'

# Review and post the comment back to GitHub
curl -X POST http://localhost:8080/review \
     -H 'Content-Type: application/json' \
     -d '{"pr_url": "https://github.com/owner/repo/pull/42", "post_comment": true}'
```

---

## Inspect local DynamoDB

```bash
# Scan all records
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  aws dynamodb scan \
    --table-name pr-review-local-memory \
    --endpoint-url http://localhost:8000

# Delete a single record
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  aws dynamodb delete-item \
    --table-name pr-review-local-memory \
    --endpoint-url http://localhost:8000 \
    --key '{"PK":{"S":"<thread_id>"},"SK":{"S":"<key>"}}'

# Full reset (drop + recreate table)
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  aws dynamodb delete-table \
    --table-name pr-review-local-memory \
    --endpoint-url http://localhost:8000
docker-compose restart dynamo-init
```

Or use the **DynamoDB Records** tab in the web UI at `http://localhost:8080`.

---

## Switching models

| Provider | `LLM_PROVIDER` | `LLM_MODEL` | Notes |
|---|---|---|---|
| Ollama small | `ollama` | `gemma4:4b` | ~2.5 GB RAM, fastest |
| Ollama large | `ollama` | `gemma4:12b` | better quality, ~7 GB RAM |
| Docker Model Runner | `docker-model` | `ai/gemma4` | Docker Desktop only |

To switch models, edit `.env` then:

```bash
# Re-pull if using Ollama
uv run python scripts/setup_local.py --provider ollama --model gemma4:12b

# Restart the server
uv run python api.py
```

---

## Stop / clean up

```bash
# Stop — keep Ollama model cache volume
docker-compose --profile ollama down      # if you used Ollama
docker-compose down                        # if you used Docker Model Runner

# Stop and delete model cache (re-download required next time)
docker-compose --profile ollama down -v
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Connection refused` on port 8000 | `docker-compose up -d` and wait ~5 s |
| `Connection refused` on port 11434 | `docker-compose --profile ollama up -d` |
| Model not found in Ollama | `uv run python scripts/setup_local.py --provider ollama` |
| `No module named 'langchain_ollama'` | `uv pip install -r requirements-v2.txt` |
| Table missing in DynamoDB | `docker-compose restart dynamo-init` |
