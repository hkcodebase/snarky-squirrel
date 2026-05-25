# Snarky Squirrel — Agentic PR Reviewer

Automated pull-request reviewer that runs a **multi-agent LangGraph pipeline**,
scores code quality and security findings on a 0–10 scale, and posts a structured
review comment to GitHub.

---

## Quick start

| Setup | LLM backend | Database | Guide |
|---|---|---|---|
| **Local — Ollama** | Ollama (Gemma) in Docker | DynamoDB Local in Docker | [docs/setup-local.md](docs/setup-local.md) |
| **Local — Docker Model Runner** | Docker Desktop built-in | DynamoDB Local in Docker | [docs/setup-local.md](docs/setup-local.md) |
| **AWS** | AWS Bedrock (Claude) | AWS DynamoDB | [docs/setup-aws.md](docs/setup-aws.md) |

---

## Architecture

```
GitHub PR ──webhook──► api.py (FastAPI)
                          │  (nginx → HTTPS in production)
              ┌───────────┴──────────────────────────────┐
              │          LangGraph agent graph            │
              │                                           │
              │   START                                   │
              │     └─► supervisor                        │
              │              ├─► code_quality  ──┐        │
              │              ├─► security       ─┤ each   │
              │              └─► pr_reviewer   ──┘ agent  │
              │              │   (sequential,   returns   │
              │              │    LLM decides order)  to  │
              │              └─► summary          supervisor
              │                    └─► END                │
              │                                           │
              │   Shared memory:  DynamoDB                │
              │   Checkpointer:   DynamoDB                │
              │   LLM:            Ollama / Bedrock        │
              └───────────────────────────────────────────┘
                              │
              ┌───────────────┴──────────────────────────┐
              │  Web UI (http://localhost:8080 local,     │
              │           https://<domain> on EC2)        │
              │  ├─ Review PR      — submit + view report │
              │  ├─ All PR Reviews — history with         │
              │  │                   DB Records + Lineage │
              │  ├─ DynamoDB       — browse/delete records│
              │  ├─ Data Lineage   — per-agent trace      │
              │  └─ Evaluation     — offline/shadow/online│
              └───────────────────────────────────────────┘
```

### Agent pipeline

| Agent | Reads from DynamoDB | Writes to DynamoDB |
|---|---|---|
| `security` | _(none — runs first)_ | `security_findings` |
| `code_quality` | `security_findings` | `code_quality_findings` |
| `pr_reviewer` | `security_findings`, `code_quality_findings` | `pr_review_findings` |
| `summary` | all three finding keys | `summary_report`, `lineage_run` |

Score formula: `10 − (CRITICAL × 5 + HIGH × 2 + MEDIUM × 1 + LOW × 0.5)`  
A CRITICAL finding sets `should_block = true`.

---

## Repository layout

```
snarky-squirrel/
├── api.py                   FastAPI server — all HTTP endpoints
├── requirements-v2.txt      Python dependencies
│
├── src/
│   ├── agents/              Four agent implementations
│   │   ├── security_agent.py
│   │   ├── code_quality_agent.py
│   │   ├── pr_reviewer_agent.py
│   │   └── summary_agent.py
│   ├── auth/
│   │   └── cognito.py           Cognito OAuth2 + JWT validation
│   ├── github/
│   │   └── client.py            GitHub API helpers (webhooks, comments, diffs)
│   ├── graph/
│   │   └── pr_review_graph.py   LangGraph state machine + LLM factory
│   └── tools/
│       └── dynamo_memory.py     DynamoDB memory store + LangGraph checkpointer
│
├── templates/
│   ├── index.html           Web UI (vanilla JS, light/dark theme)
│   └── styles.css           External stylesheet with CSS custom properties
│
├── infra/
│   └── dev/                 Terraform — AWS DynamoDB, IAM, EC2, Cognito
│       ├── providers.tf
│       ├── variables.tf
│       ├── dynamodb.tf
│       ├── iam.tf
│       ├── ec2.tf               EC2 instance + security group + SSM access
│       ├── cognito.tf           Cognito User Pool + OAuth2 App Client
│       ├── route53.tf           DNS A record for custom domain
│       ├── outputs.tf
│       └── terraform.tfvars.example
│
├── docs/
│   ├── setup-local.md       ← Local dev guide (Ollama / Docker Model Runner)
│   └── setup-aws.md         ← AWS guide (Bedrock + DynamoDB)
│
├── scripts/
│   └── setup_local.py       Verifies Docker services, pulls Ollama model
├── tests/
└── docker-compose.yml       DynamoDB Local + Ollama containers
```

---

## Environment variables

| Variable | Description | Local default | AWS value |
|---|---|---|---|
| `LLM_PROVIDER` | `ollama` / `docker-model` / `bedrock` | `ollama` | `bedrock` |
| `LLM_MODEL` | Model name passed to provider | `gemma4:4b` | _(ignored for bedrock)_ |
| `BEDROCK_MODEL_ID` | Bedrock model or inference-profile ARN | — | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `OLLAMA_BASE_URL` | Ollama server URL | `http://localhost:11434` | — |
| `DOCKER_MODEL_ENDPOINT` | Docker Model Runner URL | `http://localhost:12434/engines/llama.cpp/v1` | — |
| `AWS_REGION` | AWS region | `us-east-1` | `us-east-1` |
| `AWS_ACCESS_KEY_ID` | AWS credentials | `test` (local) | from Terraform output |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials | `test` (local) | from Terraform output |
| `DYNAMODB_ENDPOINT` | DynamoDB endpoint URL | `http://localhost:8000` | _(empty → real AWS)_ |
| `DYNAMODB_TABLE` | Table name | `pr-review-local-memory` | `pr-review-memory-dev` |
| `GITHUB_TOKEN` | GitHub PAT for fetching PR data | optional (local) | required |
| `WEBHOOK_SECRET` | HMAC secret for GitHub webhooks | `skip` | real secret |

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/review` | Review a PR by URL |
| `POST` | `/webhook` | GitHub webhook receiver |
| `GET` | `/health` | Liveness + config check |
| `GET` | `/db/records` | Browse DynamoDB records |
| `DELETE` | `/db/records/{thread_id}/{key}` | Delete a record |
| `GET` | `/lineage` | List all review runs |
| `GET` | `/lineage/detail?thread_id=` | Full agent trace for a run |
| `POST` | `/eval/run` | Offline or shadow evaluation |
| `POST` | `/eval/feedback` | Submit thumbs up/down |
| `GET` | `/eval/metrics` | Aggregate online metrics |
| `GET` | `/styles.css` | Stylesheet |

---

## Security considerations

- GitHub webhook payloads validated with HMAC-SHA256
- GitHub token and webhook secret kept in `.env` (gitignored) or Secrets Manager
- IAM policy follows least privilege — DynamoDB CRUD on one table, Bedrock InvokeModel on the configured model only
- `WEBHOOK_SECRET=skip` disables HMAC validation for local testing only

---

## Approximate AWS costs

| Component | Estimate |
|---|---|
| Bedrock Claude Haiku 4.5 | ~$0.005–0.02 per review |
| DynamoDB (on-demand) | < $0.01 per review |
| Lambda (if deployed) | ~$0.015 per review |

**Total: roughly $0.05–0.12 per PR review** depending on diff size and model.
