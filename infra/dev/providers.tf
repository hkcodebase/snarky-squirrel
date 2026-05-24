terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Uncomment and fill in your bucket/key/region before running terraform init.
  backend "s3" {
    encrypt        = true
    use_lockfile   = true # enable S3 native locking

    #   bucket = "my-terraform-state-bucket"
    #   key    = "snarky-squirrel/terraform.tfstate"
    #   region = "us-east-1"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "snarky-squirrel"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
