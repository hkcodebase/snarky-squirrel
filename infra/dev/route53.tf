# ═══════════════════════════════════════════════════════════════════════════════
# Route53 — subdomain record for the EC2 instance (only when create_ec2 = true)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Creates an A record: <subdomain_name>.<route53_zone_name> → EC2 public IP
#
# Prerequisites:
#   • A public hosted zone for route53_zone_name must already exist in Route53.
#   • create_ec2 must be true.
#
# Usage (terraform.tfvars):
#   create_ec2        = true
#   route53_zone_name = "hemantkumar.dev"
#   subdomain_name    = "snarky"
# ═══════════════════════════════════════════════════════════════════════════════

data "aws_route53_zone" "main" {
  count        = var.create_ec2 && var.route53_zone_name != "" ? 1 : 0
  name         = var.route53_zone_name
  private_zone = false
}

resource "aws_route53_record" "app" {
  count   = var.create_ec2 && var.route53_zone_name != "" ? 1 : 0
  zone_id = data.aws_route53_zone.main[0].zone_id
  name    = "${var.subdomain_name}.${var.route53_zone_name}"
  type    = "A"
  ttl     = 60
  records = [aws_instance.pr_reviewer[0].public_ip]
}
