# Every knob is here. Set the ones without a default in terraform.tfvars.

variable "project" {
  description = "Name prefix for all resources."
  type        = string
  default     = "knowledge-pipeline"
}

variable "region" {
  description = "AWS region. Everything lives here."
  type        = string
  default     = "us-east-1"
}

variable "domain" {
  description = "Your apex domain, already a hosted zone in Route 53."
  type        = string
  default     = "madebyphil.com"
}

variable "subdomain" {
  description = "Subdomain the dashboard answers on: <subdomain>.<domain>."
  type        = string
  default     = "pipeline"
}

variable "raw_bucket_name" {
  description = "GLOBALLY-unique S3 bucket name for the raw store + Litestream backups."
  type        = string
  # No default: bucket names are globally unique across ALL of AWS, so you must
  # pick one. e.g. "madebyphil-knowledge-pipeline".
}

variable "instance_type" {
  description = "EC2 size for the always-on control box. t3.small (2 vCPU/2GB) is a cheap start; bump to t3.medium if it's tight."
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  description = "Root EBS volume size (GB). Holds intermediates + the vault + Docker images."
  type        = number
  default     = 40
}

variable "ssh_public_key_path" {
  description = "Path to the PUBLIC half of the SSH key you'll connect with."
  type        = string
  default     = "~/.ssh/knowledge-pipeline.pub"
}

variable "my_ip_cidr" {
  description = "Your public IP as a /32 CIDR, e.g. \"203.0.113.4/32\". Locks SSH to just you. Get it with: curl ifconfig.me"
  type        = string
  # No default: this is a security control, so you must set it consciously.
}
