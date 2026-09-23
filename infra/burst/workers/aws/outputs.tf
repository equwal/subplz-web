output "workers" {
  description = "Each worker on AWS and its public IPv4 address."
  value       = { for name, i in aws_instance.worker : name => i.public_ip }
}
