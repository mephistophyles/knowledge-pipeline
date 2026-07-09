# Use the account's default VPC + subnets (plan §14.4: "VPC-lite or default VPC").
# Simple, free, and fine for a single box.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Firewall for the box. SSH is locked to your IP; 80/443 are open so the reverse
# proxy can serve the dashboard and Let's Encrypt can issue certs.
resource "aws_security_group" "box" {
  name        = "${var.project}-box"
  description = "Knowledge pipeline control box"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH (you only)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  ingress {
    description = "HTTP (proxy + ACME cert challenges)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS (dashboard)"
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
}
