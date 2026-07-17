# The always-on tier-1 control box (plan §14.1): orchestrator, workers, dashboard,
# Litestream. Heavy audio is a separate on-demand GPU burst (build step 4), not here.

# Latest Ubuntu 22.04 LTS (amd64) image, published by Canonical.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# Register your SSH public key so you can log in. `pathexpand` handles the ~.
resource "aws_key_pair" "box" {
  key_name   = "${var.project}-key"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

# First-boot script: install Docker Engine + Compose plugin. We do NOT build or
# run the app here — the CD pipeline publishes an image the box later pulls.
locals {
  user_data = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    apt-get update
    curl -fsSL https://get.docker.com | sh
    usermod -aG docker ubuntu
    systemctl enable --now docker
    mkdir -p /opt/${var.project}
    chown ubuntu:ubuntu /opt/${var.project}
  EOT
}

resource "aws_instance" "box" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.box.id]
  key_name               = aws_key_pair.box.key_name
  iam_instance_profile   = aws_iam_instance_profile.box.name
  user_data              = local.user_data

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
  }

  tags = {
    Name = "${var.project}-box"
  }
}

# A stable public IP that survives stop/start, so the DNS record never changes.
resource "aws_eip" "box" {
  domain   = "vpc"
  instance = aws_instance.box.id
  tags = {
    Name = "${var.project}-eip"
  }
}
