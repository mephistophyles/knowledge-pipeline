# Printed after `terraform apply` — your handles into the running system.

output "dashboard_url" {
  description = "Where the dashboard will answer once the proxy + app are up."
  value       = "https://${var.subdomain}.${var.domain}"
}

output "public_ip" {
  description = "The box's stable public IP."
  value       = aws_eip.box.public_ip
}

output "ssh_command" {
  description = "Copy-paste to log in."
  value       = "ssh -i ${trimsuffix(var.ssh_public_key_path, ".pub")} ubuntu@${aws_eip.box.public_ip}"
}

output "s3_bucket" {
  description = "Raw store + Litestream bucket."
  value       = aws_s3_bucket.raw.id
}
