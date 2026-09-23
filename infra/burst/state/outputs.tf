# What the worker modules read (through terraform_remote_state).
output "worker" {
  description = "Settings of a burst worker, on any cloud."
  sensitive   = true
  value = {
    web_server_ip      = var.web_server_ip
    ssh_port           = var.ssh_port
    known_hosts        = local.known_hosts
    tunnel_private_key = tls_private_key.tunnel.private_key_openssh
    database_url       = local.database_url
    redis_url          = local.redis_url
    bucket             = cloudflare_r2_bucket.files.name
    s3_prefix          = "jobs/"
    storage_env        = local.storage_env
  }
}

# The lines to add to /opt/subplz-web/.env on the web server when burst mode goes on.
output "web_env" {
  description = "Settings of the app on the web server (append to /opt/subplz-web/.env)."
  sensitive   = true
  value       = <<-EOT
    SUBPLZ_WEB_QUEUE_BACKEND=redis
    SUBPLZ_WEB_REDIS_URL=${local.redis_url}
    SUBPLZ_WEB_DATABASE_URL=${local.database_url}
    SUBPLZ_WEB_STORAGE_BACKEND=s3
    SUBPLZ_WEB_S3_BUCKET=${cloudflare_r2_bucket.files.name}
    SUBPLZ_WEB_S3_PREFIX=jobs/
  EOT
}

# boto3 reads these from the process environment, not from .env: they go to
# /etc/subplz-storage.env, which the systemd units load.
output "storage_env" {
  description = "Storage keys for the web server (write to /etc/subplz-storage.env, mode 0600)."
  sensitive   = true
  value       = local.storage_env
}

output "rclone_env" {
  description = "Environment for rclone: the one-time copy of the files, and the nightly Postgres backup."
  sensitive   = true
  value       = <<-EOT
    RCLONE_CONFIG_R2_TYPE=s3
    RCLONE_CONFIG_R2_PROVIDER=Cloudflare
    RCLONE_CONFIG_R2_ENDPOINT=${local.s3_endpoint}
    RCLONE_CONFIG_R2_ACCESS_KEY_ID=${local.s3_key_id}
    RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=${local.s3_secret}
  EOT
}

output "bucket" {
  value = cloudflare_r2_bucket.files.name
}
