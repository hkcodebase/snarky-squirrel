# Snarky Squirrel — Agentic PR Reviewer

Automated pull-request reviewer that runs a **multi-agent LangGraph pipeline**,
scores code quality and security findings on a 0–10 scale, and optionally posts a
structured review comment to GitHub.

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
                        ┌──────────────────────────────────────────┐
                        │           AWS Cognito                     │
                        │  Hosted UI (OAuth2) + RS256 JWT tokens   │
                        └───────────────┬──────────────────────────┘
                                        │  auth cookies
User Browser ──HTTPS──► api.py (FastAPI + nginx)
                          │
              ┌───────────┴──────────────────────────────────────┐
              │  Auth layer                                        │
              │  ├─ /auth/login  /auth/callback  /auth/logout     │
              │  └─ JWT validated on every protected request       │
              │                                                    │
              │  Web UI (Vanilla JS — single-page app)             │
              │  ├─ Review PR      — submit URL, view report      │
              │  ├─ All PR Reviews — paginated history + lineage  │
              │  ├─ Data Lineage   — per-run agent trace sidebar  │
              │  ├─ Evaluation     — offline / shadow / online    │
              │  └─ Admin          — users, invites (admin only)  │
              └───────────┬────────────────────────────────────────┘
                          │  POST /review
              ┌───────────▼──────────────────────────────────────┐
              │          LangGraph agent graph                     │
              │                                                    │
              │   START                                            │
              │     └─► supervisor                                 │
              │              ├─► security      writes findings     │
              │              ├─► code_quality  reads + writes      │
              │              └─► pr_reviewer   reads + writes      │
              │              │   (LLM decides execution order)     │
              │              └─► summary       aggregates → END    │
              │                                                    │
              │   Shared memory:  DynamoDB (per-thread KV store)  │
              │   Checkpointer:   DynamoDB (LangGraph state)       │
              │   LLM:            Ollama / Docker Model / Bedrock  │
              └───────────┬────────────────────────────────────────┘
                          │
              ┌───────────▼──────────────────────────────────────┐
              │  DynamoDB (single-table design)                    │
              │                                                    │
              │  PK=thread_id / SK=record_type                    │
              │  GSI SK-index (hash=SK, range=created_at)         │
              │  ├─ agent findings   TTL 72 h                      │
              │  ├─ lineage_run      queried by /lineage           │
              │  ├─ eval_feedback    queried by /eval/metrics      │
              │  ├─ user_settings    PK=user:{email}               │
              │  └─ invite requests  PK=invite_request:{email}     │
              └───────────────────────────────────────────────────┘
                          │
              ┌───────────▼──────────────────────────────────────┐
              │  GitHub API                                        │
              │  ├─ Fetch PR diff + metadata                      │
              │  └─ Post review comment (optional, token-gated)   │
              └───────────────────────────────────────────────────┘
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
│   ├── index.html           Web UI (vanilla JS, responsive, light/dark theme)
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
│       ├── user_data.sh.tpl     EC2 bootstrap script (env vars, systemd service)
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

### Core

| Variable | Description | Local default | AWS value |
|---|---|---|---|
| `LLM_PROVIDER` | `ollama` / `docker-model` / `bedrock` | `ollama` | `bedrock` |
| `LLM_MODEL` | Model name passed to provider | `gemma4:4b` | _(ignored for bedrock)_ |
| `BEDROCK_MODEL_ID` | Bedrock model or inference-profile ARN | — | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `OLLAMA_BASE_URL` | Ollama server URL | `http://localhost:11434` | — |
| `DOCKER_MODEL_ENDPOINT` | Docker Model Runner URL | `http://localhost:12434/engines/llama.cpp/v1` | — |
| `AWS_REGION` | AWS region | `us-east-1` | `us-east-1` |
| `AWS_ACCESS_KEY_ID` | AWS credentials | `test` (local) | from instance role |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials | `test` (local) | from instance role |
| `DYNAMODB_ENDPOINT` | DynamoDB endpoint URL | `http://localhost:8000` | _(empty → real AWS)_ |
| `DYNAMODB_TABLE` | Table name | `pr-review-local-memory` | `pr-review-memory-dev` |
| `GITHUB_TOKEN` | GitHub PAT for fetching PR data and posting comments | optional (local) | required |
| `WEBHOOK_SECRET` | HMAC secret for GitHub webhooks | `skip` | real secret |

### Auth (Cognito)

| Variable | Description |
|---|---|
| `COGNITO_USER_POOL_ID` | Cognito User Pool ID (e.g. `us-east-1_AbCdEfGhI`) |
| `COGNITO_CLIENT_ID` | App client ID |
| `COGNITO_CLIENT_SECRET` | App client secret |
| `COGNITO_DOMAIN` | Hosted UI domain prefix (e.g. `pr-reviewer-dev`) |
| `APP_URL` | Public base URL for OAuth2 redirect (e.g. `https://pr.example.com`) |

> Leave all `COGNITO_*` vars unset for local development — auth is bypassed automatically.

### Admin

| Variable | Description |
|---|---|
| `ADMIN_EMAILS` | Comma-separated list of email addresses that have admin access (e.g. `alice@example.com,bob@example.com`) |

---

## API endpoints

### Review

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/review` | required | Run the agent pipeline on a PR URL |
| `GET` | `/review/{thread_id}` | required | Fetch a single review result |
| `DELETE` | `/review/{thread_id}` | admin | Delete all DynamoDB records for a review run |
| `POST` | `/webhook` | HMAC | GitHub webhook receiver |
| `GET` | `/health` | public | Liveness + config check |

### Lineage & Evaluation

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/lineage` | required | Paginated list of review runs (`?cursor=&limit=`) |
| `GET` | `/lineage/detail?thread_id=` | required | Full per-agent trace for one run |
| `POST` | `/eval/run` | required | Offline or shadow evaluation |
| `POST` | `/eval/feedback` | required | Submit thumbs up/down for a review |
| `GET` | `/eval/metrics` | required | Aggregate online evaluation metrics |

### DynamoDB browser

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/db/records` | required | Browse raw DynamoDB records |
| `DELETE` | `/db/records/{thread_id}/{key}` | required | Delete one record by key |

### Auth

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/auth/login` | public | Redirect to Cognito Hosted UI |
| `GET` | `/auth/callback` | public | OAuth2 callback — sets session cookie |
| `GET` | `/auth/logout` | public | Clear session cookie |
| `GET` | `/auth/me` | public | Current user info `{email, is_admin}` or `null` |

### User settings

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/user/settings` | public | Fetch persisted user settings (e.g. last PR URL) |

### Invite / access requests

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/invite/request` | public | Submit an access request by email |
| `GET` | `/admin/invite-requests` | admin | List all pending access requests |
| `DELETE` | `/admin/invite-requests/{email}` | admin | Dismiss an access request |

### Admin — user management

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/admin/users` | admin | List all Cognito users |
| `POST` | `/admin/users` | admin | Invite a user (sends temp-password email via Cognito) |
| `DELETE` | `/admin/users/{email}` | admin | Remove a Cognito user |

### Static

| Method | Path | Description |
|---|---|---|
| `GET` | `/styles.css` | Stylesheet |

---

## DynamoDB schema

Single-table design. All records share the same table and are distinguished by `SK`.

| Attribute | Type | Description |
|---|---|---|
| `PK` | String (hash key) | Thread ID (`pr-{owner}-{repo}-{num}-{hash}`), or `user:{email}`, or `invite_request:{email}` |
| `SK` | String (range key) | Record type: `security_findings`, `code_quality_findings`, `pr_review_findings`, `summary_report`, `lineage_run`, `__checkpoint__`, `user_settings`, `request` |
| `value` | String | JSON payload written by each agent or API handler |
| `ttl` | Number | Unix epoch expiry (72 hours default, enforced by DynamoDB TTL) |
| `created_at` | String | ISO-8601 UTC timestamp — written on every `put()` |

### GSI: `SK-index`

| Attribute | Role |
|---|---|
| `SK` | Hash key — query all records of a given type without a table scan |
| `created_at` | Range key — results in newest-first order |

The GSI replaces all `scan + FilterExpression` patterns. Endpoints that need all
records of a type (e.g. `/eval/metrics`) paginate through the GSI internally;
endpoints like `/lineage` expose the `next_cursor` token to the client for
load-on-demand pagination.

---

## Web UI

The UI is a single-page vanilla-JS app served from `templates/index.html`. It uses
a JetBrains Mono / HK color palette design system defined as CSS custom properties
in `templates/styles.css` and is fully responsive down to mobile (≤ 600 px).

### Tabs

| Tab | Description |
|---|---|
| **Review PR** | Enter a PR URL and optional GitHub token, run the pipeline, view the scored report |
| **All PR Reviews** | Paginated history of every review run with Load More; row links open the detail modal |
| **Data Lineage** | Sidebar list of runs (Load More) with per-run agent-step trace on the right |
| **Evaluation** | Submit offline / shadow runs; thumbs up/down feedback; live metric gauges |
| **Admin** _(admin only)_ | Manage Cognito users (invite / remove), view and dismiss access requests |

### Key UX details

- **GitHub token** — only required to post the review as a GitHub comment; leave blank to run a review without commenting (falls back to the server token).
- **Request access** — unauthenticated visitors see an email form to request an invite without leaving the page.
- **Last PR URL** — the most recently reviewed PR URL is persisted per-user in DynamoDB and pre-filled on next login.
- **Clickable PR links** — review history and the detail modal link directly to the GitHub PR. URLs for older records (pre-`pr_url` field) are reconstructed from `pr_repo` + `pr_number`.
- **Load More** — the past-reviews table and lineage sidebar use cursor-based pagination (`next_cursor` token) so the page never loads more than 20 items at a time.

---

## Security considerations

- **Cognito JWT** — every request to a protected endpoint validates the RS256 JWT from the session cookie against Cognito's JWKS endpoint; no JWT → redirect to login.
- **Admin access** — controlled by the `ADMIN_EMAILS` environment variable (comma-separated). Admin-only endpoints return `403` for non-admins.
- **GitHub webhook payloads** validated with HMAC-SHA256 (`WEBHOOK_SECRET`).
- **GitHub token and secrets** kept in `.env` (gitignored) or AWS Systems Manager / Secrets Manager.
- **IAM policy** follows least privilege — DynamoDB CRUD on one table, Bedrock `InvokeModel` on the configured model only, Cognito user management on the single user pool.
- **SSM Session Manager** used for EC2 shell access — no SSH key or open port 22 required.
- `WEBHOOK_SECRET=skip` disables HMAC validation for local testing only.

---

## Approximate AWS costs

| Component | Estimate |
|---|---|
| Bedrock Claude Haiku 4.5 | ~$0.005–0.02 per review |
| DynamoDB (on-demand) | < $0.01 per review |
| EC2 t3.micro | ~$8/month (always-on) |
| Cognito | free tier (< 50 MAU) |

**Total: roughly $0.05–0.12 per PR review** depending on diff size and model.
