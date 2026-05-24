#!/bin/bash
# =============================================================================
# user_data.sh.tpl — EC2 bootstrap script for the PR Reviewer API server.
#
# Template variables injected by Terraform templatefile():
#   bedrock_model_id         : AWS Bedrock model ID (e.g. us.anthropic.claude-haiku-4-5-...)
#   aws_region               : AWS region for API calls (e.g. us-east-1)
#   dynamodb_table           : DynamoDB table name for agent memory storage
#   ssm_token_param          : SSM path for GitHub PAT (SecureString)
#   ssm_cognito_secret_param : SSM path for Cognito client secret (SecureString)
#   cognito_user_pool_id     : Cognito User Pool ID
#   cognito_client_id        : Cognito App Client ID
#   cognito_domain           : Cognito Hosted UI domain (no https://)
#   app_url                  : Public base URL of the app (e.g. https://snarky.hemantkumar.dev)
#   fqdn                     : Fully qualified domain name for nginx + Let's Encrypt
#   certbot_email            : Email address for Let's Encrypt certificate notifications
# =============================================================================
set -euo pipefail
exec > >(tee /var/log/pr-reviewer-init.log | logger -t pr-reviewer-init) 2>&1

# ── Helper: run a named step with clear success/failure output ─────────────────
run_step() {
  local name="$1"; shift
  echo "--- Starting: $name ---"
  if "$@"; then
    echo "✓ $name completed"
  else
    echo "✗ $name FAILED — aborting bootstrap"
    exit 1
  fi
}

echo "=== PR Reviewer bootstrap starting ==="

# Security patches only — faster and more reproducible than a full update
run_step "System security update" dnf update -y --security

run_step "Install packages" dnf install -y git rsync nginx certbot python3-certbot-nginx

# Install uv (user-data runs as root)
run_step "Install uv" bash -c 'curl -LsSf https://astral.sh/uv/install.sh | env HOME=/root sh'
export PATH="/root/.local/bin:$PATH"
echo 'export PATH="/root/.local/bin:$PATH"' >> /root/.bashrc

# Install Python 3.12 via uv (not available in AL2023 dnf repos)
run_step "Install Python 3.12" /root/.local/bin/uv python install 3.12

# Clone repo and checkout branch
run_step "Clone repository" git clone https://github.com/hkcodebase/snarky-squirrel.git /opt/pr-reviewer
cd /opt/pr-reviewer
run_step "Checkout feature/v2" git checkout feature/v2

# Create virtual environment and install dependencies
run_step "Create venv" /root/.local/bin/uv venv --python 3.12 /opt/pr-reviewer/.venv
run_step "Install dependencies" /root/.local/bin/uv pip install \
  --python /opt/pr-reviewer/.venv/bin/python \
  -r requirements-v2.txt

# ── Retrieve secrets from SSM at runtime (never embedded in user_data) ────────
ssm_get() {
  local param="$1"
  if [ -n "$param" ]; then
    aws ssm get-parameter --name "$param" --with-decryption \
      --query Parameter.Value --output text --region ${aws_region} 2>/dev/null || echo ""
  else
    echo ""
  fi
}

echo "--- Retrieving secrets from SSM ---"
GITHUB_TOKEN=$(ssm_get "${ssm_token_param}")
COGNITO_CLIENT_SECRET=$(ssm_get "${ssm_cognito_secret_param}")
echo "✓ Secrets retrieved"

# ── Write .env ─────────────────────────────────────────────────────────────────
cat > /opt/pr-reviewer/.env << 'DOTENV'
LLM_PROVIDER=bedrock
BEDROCK_MODEL_ID=${bedrock_model_id}
AWS_REGION=${aws_region}
DYNAMODB_TABLE=${dynamodb_table}
DYNAMODB_ENDPOINT=
WEBHOOK_SECRET=skip
COGNITO_USER_POOL_ID=${cognito_user_pool_id}
COGNITO_CLIENT_ID=${cognito_client_id}
COGNITO_DOMAIN=${cognito_domain}
APP_URL=${app_url}
DOTENV

# Append SSM-retrieved secrets (never hardcode these in the heredoc)
echo "GITHUB_TOKEN=$GITHUB_TOKEN" >> /opt/pr-reviewer/.env
echo "COGNITO_CLIENT_SECRET=$COGNITO_CLIENT_SECRET" >> /opt/pr-reviewer/.env
chmod 600 /opt/pr-reviewer/.env
echo "✓ .env written"

# ── Register systemd service ───────────────────────────────────────────────────
cat > /etc/systemd/system/pr-reviewer.service << 'SERVICE'
[Unit]
Description=Snarky Squirrel PR Reviewer API
After=network.target

[Service]
Type=simple
User=root
Environment=HOME=/root
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
WorkingDirectory=/opt/pr-reviewer
EnvironmentFile=/opt/pr-reviewer/.env
ExecStart=/opt/pr-reviewer/.venv/bin/python api.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

run_step "Enable pr-reviewer service" bash -c '
  systemctl daemon-reload
  systemctl enable pr-reviewer
  systemctl start pr-reviewer
'

# ── nginx reverse proxy + Let's Encrypt SSL ────────────────────────────────────
FQDN="${fqdn}"
EMAIL="${certbot_email}"

if [ -n "$FQDN" ] && [ -n "$EMAIL" ]; then
  echo "--- Configuring nginx for $FQDN ---"

  cat > /etc/nginx/conf.d/pr-reviewer.conf << NGINX
server {
    listen 80;
    server_name $FQDN;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
        proxy_connect_timeout 300;
    }
}
NGINX

  run_step "Start nginx" bash -c 'nginx -t && systemctl enable --now nginx'

  echo "--- Obtaining Let's Encrypt certificate (retrying until DNS propagates) ---"
  for i in $(seq 1 12); do
    if certbot --nginx -d "$FQDN" --non-interactive --agree-tos --email "$EMAIL" --redirect; then
      echo "✓ SSL certificate obtained"
      break
    fi
    echo "Certbot attempt $i/12 failed — retrying in 30s..."
    sleep 30
  done
else
  echo "! Skipping nginx/SSL setup (fqdn or certbot_email not set)"
fi

echo "=== Bootstrap complete. Check: journalctl -u pr-reviewer -f ==="
