# ═══════════════════════════════════════════════════════════════════════════════
# EC2 — API server (only when create_ec2 = true)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Creates:
#   • IAM role + instance profile (EC2 → DynamoDB + Bedrock)
#   • Security group (port 22 SSH + port 8080 API)
#   • EC2 instance (Amazon Linux 2023, uv, systemd service)
#
# Usage:
#   Set create_ec2 = true in terraform.tfvars, then:
#     terraform apply
#     ssh -i ~/.ssh/<key>.pem ec2-user@<ec2_public_ip>
#
# Or without SSH key (SSM Session Manager):
#     aws ssm start-session --target <instance_id> --profile <your-sso-profile>
# ═══════════════════════════════════════════════════════════════════════════════

# ── Data sources ──────────────────────────────────────────────────────────────

data "aws_vpc" "default" {
  count   = var.create_ec2 ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.create_ec2 ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
  filter {
    name   = "map-public-ip-on-launch"
    values = ["true"]
  }
}

# Latest Amazon Linux 2023 (x86_64)
data "aws_ami" "al2023" {
  count       = var.create_ec2 ? 1 : 0
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

# ── IAM role + instance profile ───────────────────────────────────────────────

resource "aws_iam_role" "pr_reviewer_ec2" {
  count = var.create_ec2 ? 1 : 0
  name  = "pr-reviewer-ec2-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "pr-reviewer-ec2-${var.environment}" }
}

# Reuse the same DynamoDB + Bedrock policy created in iam.tf
resource "aws_iam_role_policy_attachment" "pr_reviewer_ec2_app" {
  count      = var.create_ec2 ? 1 : 0
  role       = aws_iam_role.pr_reviewer_ec2[0].name
  policy_arn = aws_iam_policy.pr_reviewer.arn
}

# SSM Session Manager — remote shell without SSH key
resource "aws_iam_role_policy_attachment" "pr_reviewer_ec2_ssm" {
  count      = var.create_ec2 ? 1 : 0
  role       = aws_iam_role.pr_reviewer_ec2[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "pr_reviewer" {
  count = var.create_ec2 ? 1 : 0
  name  = "pr-reviewer-${var.environment}"
  role  = aws_iam_role.pr_reviewer_ec2[0].name
}

# ── Security group ────────────────────────────────────────────────────────────

resource "aws_security_group" "pr_reviewer" {
  count       = var.create_ec2 ? 1 : 0
  name        = "pr-reviewer-${var.environment}"
  description = "PR Reviewer API server - SSH + API port"
  vpc_id      = data.aws_vpc.default[0].id

  # SSH removed — use SSM Session Manager for shell access (no key needed):
  #   aws ssm start-session --target <instance-id> --profile <profile>

  ingress {
    # Port 80 must be open to 0.0.0.0/0 for Let's Encrypt ACME HTTP-01 challenges.
    description = "HTTP - required for Lets Encrypt ACME validation"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    # Port 443 is the public HTTPS endpoint — intentionally open to all.
    description = "HTTPS - public web application"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "pr-reviewer-${var.environment}" }
}

# ── EC2 instance ──────────────────────────────────────────────────────────────

resource "aws_instance" "pr_reviewer" {
  count = var.create_ec2 ? 1 : 0

  ami                         = data.aws_ami.al2023[0].id
  instance_type               = var.ec2_instance_type
  key_name                    = var.ec2_key_name != "" ? var.ec2_key_name : null
  iam_instance_profile        = aws_iam_instance_profile.pr_reviewer[0].name
  subnet_id                   = data.aws_subnets.default[0].ids[0]
  vpc_security_group_ids      = [aws_security_group.pr_reviewer[0].id]
  associate_public_ip_address = true

  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  # Bootstrap script is in user_data.sh.tpl — templatefile() injects Terraform
  # variables so the script runs cleanly without inline heredoc nesting issues.
  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    bedrock_model_id = var.bedrock_model_id
    aws_region       = var.aws_region
    dynamodb_table   = aws_dynamodb_table.pr_review_memory.name
    # Secrets are NOT passed here — retrieved at runtime from SSM to avoid
    # embedding them in user_data (visible in EC2 instance metadata).
    ssm_token_param          = var.create_ec2 && var.github_token != "" ? aws_ssm_parameter.github_token[0].name : ""
    ssm_cognito_secret_param = var.enable_cognito ? aws_ssm_parameter.cognito_client_secret[0].name : ""
    # Non-sensitive Cognito config — safe to pass directly
    cognito_user_pool_id = var.enable_cognito ? aws_cognito_user_pool.pr_reviewer[0].id : ""
    cognito_client_id    = var.enable_cognito ? aws_cognito_user_pool_client.pr_reviewer[0].id : ""
    cognito_domain = var.enable_cognito ? "${aws_cognito_user_pool_domain.pr_reviewer[0].domain}.auth.${var.aws_region}.amazoncognito.com" : ""
    app_url       = var.app_url
    fqdn          = var.route53_zone_name != "" ? "${var.subdomain_name}.${var.route53_zone_name}" : ""
    certbot_email = var.certbot_email
  })

  tags = {
    Name        = "pr-reviewer-${var.environment}"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# ── SSM Parameter — GitHub token stored as SecureString ───────────────────────
# Retrieved at runtime by the EC2 instance so the token is never embedded
# in user_data (which is visible via EC2 instance metadata).

resource "aws_ssm_parameter" "github_token" {
  count       = var.create_ec2 && var.github_token != "" ? 1 : 0
  name        = "/pr-reviewer/${var.environment}/github-token"
  description = "GitHub PAT for the PR Reviewer EC2 instance"
  type        = "SecureString"
  value       = var.github_token

  tags = { Environment = var.environment, ManagedBy = "terraform" }
}
