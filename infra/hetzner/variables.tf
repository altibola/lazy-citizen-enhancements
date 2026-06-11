variable "hcloud_token" {
  description = "Hetzner Cloud API token (project-scoped, read+write)."
  type        = string
  sensitive   = true
}

variable "server_name" {
  description = "Name of the ephemeral build server."
  type        = string
  default     = "lce-build"
}

variable "server_type" {
  description = "Hetzner server type. cpx31 = 4 vCPU / 8 GB RAM (~0.03 EUR/h) — enough for CDN download + unforge + generation."
  type        = string
  default     = "cpx31"
}

variable "location" {
  description = "Hetzner location (fsn1, nbg1, hel1, ash, hil)."
  type        = string
  default     = "fsn1"
}

variable "image" {
  description = "OS image for the build server."
  type        = string
  default     = "ubuntu-24.04"
}

variable "ssh_public_key" {
  description = "SSH public key (OpenSSH format) authorized to log in as root."
  type        = string
}
