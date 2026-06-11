output "server_ip" {
  description = "Public IPv4 of the build server."
  value       = hcloud_server.build.ipv4_address
}

output "server_name" {
  description = "Name of the build server."
  value       = hcloud_server.build.name
}

output "server_status" {
  description = "Current server status."
  value       = hcloud_server.build.status
}
