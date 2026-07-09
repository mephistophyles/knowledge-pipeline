# Pins Terraform + the AWS provider so `apply` behaves the same everywhere.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # State lives locally for now (see infra/README.md). It records what Terraform
  # created and can contain sensitive values — it is gitignored, never committed.
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}
