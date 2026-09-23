output "workers" {
  description = "Each worker on Hetzner and its public IPv4 address."
  value       = { for name, s in hcloud_server.worker : name => s.ipv4_address }
}
