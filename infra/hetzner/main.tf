# Ephemeral Hetzner Cloud build host for lazy-citizen-enhancements.
#
# Provisions a single Ubuntu server with everything the pipeline needs
# (Python, mono for unforge.exe, git). The server is meant to live only for
# the duration of one build: apply → run remote_build.sh → destroy.
#
# Usage (Git Bash or CI):
#   scripts/provision.sh    # terraform init + apply
#   scripts/run_build.sh    # run the pipeline on the server
#   scripts/destroy.sh      # terraform destroy

terraform {
  required_version = ">= 1.5"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
  }
}

provider "hcloud" {
  token = var.hcloud_token
}

resource "hcloud_ssh_key" "build" {
  name       = "${var.server_name}-key"
  public_key = var.ssh_public_key
}

# SSH-only ingress; all egress open (CDN + GitHub downloads).
resource "hcloud_firewall" "build" {
  name = "${var.server_name}-fw"

  rule {
    description = "SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }
}

resource "hcloud_server" "build" {
  name         = var.server_name
  image        = var.image
  server_type  = var.server_type
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.build.id]
  firewall_ids = [hcloud_firewall.build.id]
  user_data    = file("${path.module}/cloud-init.yml")

  labels = {
    purpose   = "lce-build"
    ephemeral = "true"
  }
}
