# AWS setup — Bedrock (LLM) + DynamoDB (memory)

Run the PR review system locally while pointing both the LLM and the  
database at **real AWS services**. No Docker, no local containers needed.

---

## Prerequisites

| Tool | Notes |
|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.5+ | Fast Python package manager — `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Terraform](https://developer.hashicorp.com/terraform/install) ≥ 1.6 | provisions DynamoDB table + IAM policy |
| AWS CLI v2 | SSO login and manual DynamoDB inspection |
| AWS SSO profile | with permissions to create DynamoDB tables and IAM policies |

> **Bedrock model access must be enabled manually** in the AWS console —  
> Terraform cannot request access on your behalf.  
> Go to **AWS Console → Bedrock → Model access → Manage model access**  
> and enable the model you plan to use (e.g. **Claude Haiku 4.5** or **Claude Sonnet 4.5**).

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

> All subsequent commands use `uv run python …` so you don't need to activate the venv.  
> If you prefer to activate: `source .venv/bin/activate` (macOS/Linux) or `.\.venv\Scripts\Activate.ps1` (Windows).

---

## Step 2 — Log in with AWS SSO

```bash
aws sso login --profile <your-sso-profile>
```

Verify the session is active:

```bash
aws sts get-caller-identity --profile <your-sso-profile>
```

---

## Step 3 — Provision AWS infrastructure with Terraform

```bash
cd infra/dev

# Copy and edit variables
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:

```hcl
aws_region        = "us-east-1"          # region where Bedrock models are available
environment       = "dev"
dynamo_table_name = "pr-review-memory"   # Terraform appends "-${environment}" → pr-review-memory-dev

# Must match the model you enabled in Bedrock console
bedrock_model_id  = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Your SSO profile name — included in the env_block output for convenience
aws_profile       = "<your-sso-profile>"

# false = SSO / existing role (default). Set true only if you need a dedicated IAM user + access key.
create_iam_user   = false
```

```bash
# Initialise providers
terraform init

# Preview what will be created
terraform plan

# Apply (creates DynamoDB table and IAM policy)
terraform apply
```

Resources created:
- `aws_dynamodb_table.pr_review_memory` — PAY_PER_REQUEST, TTL enabled, SK-GSI, PITR
- `aws_iam_policy.pr_reviewer` — DynamoDB CRUD + `bedrock:InvokeModel`

After apply, attach the policy to your SSO role:

```bash
# Get the policy ARN
terraform output iam_policy_arn

# Attach to the IAM role used by your SSO profile
aws iam attach-role-policy \
  --role-name <your-sso-role-name> \
  --policy-arn <iam_policy_arn>
```

---

## Step 4 — Configure local environment

```bash
cd ../..   # back to repo root

# Option A — paste the ready-made block directly into .env
terraform -chdir=infra/dev output -raw env_block >> .env

# Option B — copy the example and edit manually
cp .env.aws.example .env
```

The `.env` should contain:

```dotenv
LLM_PROVIDER=bedrock
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
AWS_REGION=us-east-1
AWS_PROFILE=<your-sso-profile>
DYNAMODB_TABLE=pr-review-memory-dev
DYNAMODB_ENDPOINT=
GITHUB_TOKEN=ghp_your_token_here
WEBHOOK_SECRET=skip
```

> **`DYNAMODB_ENDPOINT` must be empty (not set to localhost).**  
> An empty value tells boto3 to use the real regional endpoint.

---

## Step 5 — Run the server

```bash
# Ensure your SSO session is active before starting
aws sso login --profile <your-sso-profile>

uv run python api.py
# → http://localhost:8080
```

The server auto-reloads on any `.py` file change (`reload=True`).  
For `templates/index.html` or `styles.css` changes, hard-refresh the browser (`Ctrl+Shift+R`).

Health check:

```bash
curl http://localhost:8080/health
# {"status":"ok","llm_provider":"bedrock","llm_model":"anthropic.claude-...","dynamodb_endpoint":""}
```

Startup log (success):

```
INFO  DB   ✓  Using AWS DynamoDB  region=us-east-1  table=pr-review-memory-dev
INFO  DB   ✓  Table 'pr-review-memory-dev' exists
INFO  LLM  ✓  AWS Bedrock — skipping connectivity check  model=us.anthropic.claude-haiku-4-5-20251001-v1:0
INFO  Uvicorn running on http://0.0.0.0:8080
```

---

## Running a review

### From the web UI

Open your app URL (e.g. `https://snarky.hemantkumar.dev`), paste a GitHub PR URL, click **Review PR**.

### From curl

```bash
# Trigger a review
curl -X POST http://localhost:8080/review \
     -H 'Content-Type: application/json' \
     -d '{"pr_url": "https://github.com/owner/repo/pull/42"}'

# Review and post comment back to GitHub
curl -X POST http://localhost:8080/review \
     -H 'Content-Type: application/json' \
     -d '{"pr_url": "https://github.com/owner/repo/pull/42", "post_comment": true}'
```

---

## Inspect AWS DynamoDB

```bash
# Scan all records in the table
aws dynamodb scan \
  --table-name pr-review-memory-dev \
  --region us-east-1 \
  --profile <your-sso-profile>

# Query all records for a specific PR run
aws dynamodb query \
  --table-name pr-review-memory-dev \
  --region us-east-1 \
  --profile <your-sso-profile> \
  --key-condition-expression "PK = :tid" \
  --expression-attribute-values '{":tid":{"S":"pr-owner-repo-42-a1b2c3d4"}}'

# Delete a single record
aws dynamodb delete-item \
  --table-name pr-review-memory-dev \
  --region us-east-1 \
  --profile <your-sso-profile> \
  --key '{"PK":{"S":"<thread_id>"},"SK":{"S":"<key>"}}'
```

Or use the **DynamoDB Records** tab in the web UI at `http://localhost:8080`.

---

## Switching Bedrock models

Edit `.env` and update `BEDROCK_MODEL_ID`, then restart the server.  
Make sure the new model is enabled in **Bedrock → Model access** first.

| Model | ID | Speed | Cost |
|---|---|---|---|
| Claude Haiku 4.5 — cross-region (**default**) | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | fast | ~$0.01/review |
| Claude Sonnet 4.5 | `anthropic.claude-sonnet-4-5-20250514-v1:0` | medium | ~$0.06/review |

Cross-region inference profiles (`us.anthropic.*`) route automatically across  
`us-east-1`, `us-east-2`, `us-west-2` for higher throughput and better availability.

To update the model tracked by Terraform (for the IAM policy ARN):

```bash
cd infra/dev
# Edit terraform.tfvars: bedrock_model_id = "anthropic.claude-sonnet-4-5-20250514-v1:0"
terraform apply
```

---

## Deploy to EC2 (optional)

Run the API server on an EC2 instance instead of your laptop.

### Step 1 — Enable EC2 in Terraform

Edit `infra/dev/terraform.tfvars`:

```hcl
create_ec2        = true
ec2_instance_type = "t3.small"    # t3.micro for low traffic
github_token      = "ghp_..."     # your GitHub PAT

# Optional — custom domain via Route53 + Let's Encrypt
route53_zone_name = "example.com"
subdomain_name    = "pr-reviewer"
certbot_email     = "admin@example.com"
app_url           = "https://pr-reviewer.example.com"
```

```bash
cd infra/dev
terraform apply
```

Terraform outputs the instance ID and ready-to-use commands:

```
ec2_instance_id = "i-0abc1234def5678"
ec2_ssm_command = "aws ssm start-session --target i-0abc1234def5678 --profile <profile>"
app_dns_url     = "https://pr-reviewer.example.com"
```

### Step 2 — Wait for bootstrap (~3 min)

The user-data script installs uv, clones the repo, installs dependencies, and starts the `pr-reviewer` systemd service. Watch progress via SSM Session Manager (no SSH key needed):

```bash
aws ssm start-session --target i-0abc1234def5678 --profile <your-sso-profile>
# then: sudo journalctl -u pr-reviewer -f
# or:   sudo tail -f /var/log/pr-reviewer-init.log
```

### Step 3 — Deploy code updates

After making local code changes, push them to EC2 without a full Terraform cycle:

```bash
# Via SSM (no SSH key needed)
./scripts/deploy_ec2.sh ssm i-0abc1234def5678

# On a different branch
./scripts/deploy_ec2.sh ssm i-0abc1234def5678 --branch main
```

The script syncs code, reinstalls dependencies, and restarts the service.

---

## Tear down infrastructure

```bash
cd infra/dev
terraform destroy
```

This removes the DynamoDB table (all data), the IAM policy, and the EC2 instance (if created).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ExpiredTokenException` | SSO session expired — run `aws sso login --profile <your-profile>` |
| `AccessDeniedException` on Bedrock | Enable model in AWS Console → Bedrock → Model access |
| `AccessDeniedException` on DynamoDB | Attach the `iam_policy_arn` output to your SSO role |
| `ResourceNotFoundException` on table | Server auto-creates it on startup; or run `terraform apply` again |
| `No module named 'langchain_aws'` | `uv pip install -r requirements-v2.txt` |
| Wrong region for model | Set `aws_region` in `terraform.tfvars` to a region where the model is available (`us-east-1` for all Anthropic models) |
