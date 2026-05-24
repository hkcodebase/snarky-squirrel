#!/usr/bin/env bash
# =============================================================================
# deploy_ec2.sh — Push local code changes to the EC2 instance and restart
#
# Usage:
#   ./scripts/deploy_ec2.sh <ec2-public-ip> [--key ~/.ssh/my-key.pem] [--env]
#
# Options:
#   --key  <path>   Path to SSH private key (default: ~/.ssh/id_rsa)
#   --env           Also upload your local .env to the instance (optional)
#   --branch <name> Git branch to checkout on EC2 (default: feature/v2)
#
# Examples:
#   # First time — no SSH key, use SSM:
#   ./scripts/deploy_ec2.sh ssm i-0abc1234def5678
#
#   # With SSH key:
#   ./scripts/deploy_ec2.sh 3.92.45.21 --key ~/.ssh/pr-reviewer.pem
#
#   # Push code + upload .env:
#   ./scripts/deploy_ec2.sh 3.92.45.21 --key ~/.ssh/pr-reviewer.pem --env
# =============================================================================
set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────

TARGET="${1:-}"
SSH_KEY="${HOME}/.ssh/id_rsa"
UPLOAD_ENV=false
BRANCH="feature/v2"
USE_SSM=false

if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 <ec2-ip | ssm <instance-id>> [--key <path>] [--env] [--branch <name>]"
  exit 1
fi

if [[ "$TARGET" == "ssm" ]]; then
  INSTANCE_ID="${2:-}"
  if [[ -z "$INSTANCE_ID" ]]; then
    echo "Error: provide instance ID after 'ssm', e.g. ssm i-0abc1234"
    exit 1
  fi
  USE_SSM=true
  shift 2
else
  shift 1
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key)    SSH_KEY="$2";    shift 2 ;;
    --env)    UPLOAD_ENV=true; shift 1 ;;
    --branch) BRANCH="$2";    shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

REMOTE_DIR="/opt/pr-reviewer"
SSH_USER="ec2-user"

# ── Helpers ───────────────────────────────────────────────────────────────────

remote() {
  if $USE_SSM; then
    aws ssm send-command \
      --instance-ids "$INSTANCE_ID" \
      --document-name "AWS-RunShellScript" \
      --parameters "commands=[\"$*\"]" \
      --output text \
      --query "Command.CommandId"
  else
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "${SSH_USER}@${TARGET}" "$@"
  fi
}

rsync_to() {
  local src="$1" dst="$2"
  if $USE_SSM; then
    echo "  [WARN] rsync not supported over SSM — commit + git pull on the instance instead"
  else
    rsync -az --delete \
      --exclude '.git' \
      --exclude '.venv' \
      --exclude '__pycache__' \
      --exclude '*.pyc' \
      --exclude '.env' \
      --exclude 'error' \
      -e "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new" \
      "$src" "${SSH_USER}@${TARGET}:${dst}"
  fi
}

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Snarky Squirrel — EC2 Deploy                       ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
if $USE_SSM; then
  echo "  Target  : SSM → $INSTANCE_ID"
else
  echo "  Target  : $SSH_USER@$TARGET"
  echo "  SSH key : $SSH_KEY"
fi
echo "  Branch  : $BRANCH"
echo "  Upload .env : $UPLOAD_ENV"
echo ""

# ── Step 1: Sync code ─────────────────────────────────────────────────────────

echo "▶  Syncing code…"
if $USE_SSM; then
  echo "   Pulling latest from origin on the instance…"
  remote "cd ${REMOTE_DIR} && git fetch origin && git checkout ${BRANCH} && git pull origin ${BRANCH}"
else
  rsync_to "$(pwd)/" "$REMOTE_DIR/"
fi
echo "   ✓ Code synced"

# ── Step 2: Upload .env (optional) ───────────────────────────────────────────

if $UPLOAD_ENV; then
  if [[ ! -f ".env" ]]; then
    echo "   [WARN] No .env file found in current directory — skipping"
  else
    echo "▶  Uploading .env…"
    if $USE_SSM; then
      echo "   [INFO] Cannot upload files over SSM. Copy .env manually via scp or the AWS console."
    else
      scp -i "$SSH_KEY" .env "${SSH_USER}@${TARGET}:${REMOTE_DIR}/.env"
      remote "chmod 600 ${REMOTE_DIR}/.env"
      echo "   ✓ .env uploaded"
    fi
  fi
fi

# ── Step 3: Install / update Python dependencies ─────────────────────────────

echo "▶  Installing Python dependencies…"
remote "cd ${REMOTE_DIR} && /root/.local/bin/uv pip install -r requirements-v2.txt --quiet"
echo "   ✓ Dependencies installed"

# ── Step 4: Restart service ───────────────────────────────────────────────────

echo "▶  Restarting pr-reviewer service…"
remote "systemctl restart pr-reviewer"
echo "   ✓ Service restarted"

# ── Step 5: Health check (via internal curl on the instance) ──────────────────

echo "▶  Checking service health…"
sleep 5

if $USE_SSM; then
  aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["curl -sf http://localhost:8080/health && echo OK || echo FAIL"]' \
    --output text --query "Command.CommandId" > /dev/null || true
fi

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Logs    : aws ssm start-session --target ${INSTANCE_ID:-$TARGET}"
echo "            then: journalctl -u pr-reviewer -f"
echo "══════════════════════════════════════════════════════"
echo ""
