# The shared state of burst mode: Postgres and Redis on the web server, the
# file bucket (Cloudflare R2), and the key of the SSH tunnel that each burst
# worker uses to reach Postgres and Redis. Workers can come from any cloud;
# this module does not depend on one.
#
# Run it ON THE WEB SERVER, as root: the setup step changes this machine
# (see setup-web-server.sh). See ../README.md for when and how.

terraform {
  required_version = ">= 1.9"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.25"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
  # The state holds passwords and keys: /var/lib/subplz-burst, root only.
  backend "local" {}
}

# Reads CLOUDFLARE_API_TOKEN.
provider "cloudflare" {}

resource "random_password" "postgres" {
  length  = 32
  special = false
}

resource "random_password" "redis" {
  length  = 32
  special = false
}

# One key for every worker: it can open the two tunnels and nothing else.
resource "tls_private_key" "tunnel" {
  algorithm = "ED25519"
}

resource "cloudflare_r2_bucket" "files" {
  account_id = var.cloudflare_account_id
  name       = var.bucket_name
  location   = var.bucket_location
}

# The permission groups "read objects" and "write objects" of R2, found by name
# unless var.r2_permission_group_ids gives them.
data "cloudflare_api_token_permission_groups_list" "r2_read" {
  count = var.r2_permission_group_ids == null ? 1 : 0
  name  = "Workers%20R2%20Storage%20Bucket%20Item%20Read"
}

data "cloudflare_api_token_permission_groups_list" "r2_write" {
  count = var.r2_permission_group_ids == null ? 1 : 0
  name  = "Workers%20R2%20Storage%20Bucket%20Item%20Write"
}

# The app and the workers get a key for this bucket only. R2 takes the token
# id as the S3 key id, and the SHA-256 of the token value as the S3 secret.
resource "cloudflare_api_token" "files" {
  name = "${var.bucket_name}-app"
  policies = [{
    effect            = "allow"
    permission_groups = [for id in local.r2_permission_groups : { id = id }]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.files.name}" = "*"
    })
  }]
}

# Postgres and Redis on 127.0.0.1, and the tunnel user. The script is
# idempotent: a second run changes nothing.
resource "terraform_data" "web_server" {
  triggers_replace = [
    filesha256("${path.module}/setup-web-server.sh"),
    sha256(random_password.postgres.result),
    sha256(random_password.redis.result),
    tls_private_key.tunnel.public_key_openssh,
  ]

  provisioner "local-exec" {
    command = "bash ${path.module}/setup-web-server.sh"
    environment = {
      POSTGRES_PASSWORD = random_password.postgres.result
      REDIS_PASSWORD    = random_password.redis.result
      TUNNEL_PUBLIC_KEY = tls_private_key.tunnel.public_key_openssh
    }
  }
}

locals {
  r2_permission_groups = var.r2_permission_group_ids != null ? var.r2_permission_group_ids : concat(
    [for d in data.cloudflare_api_token_permission_groups_list.r2_read : d.result[0].id],
    [for d in data.cloudflare_api_token_permission_groups_list.r2_write : d.result[0].id],
  )
  s3_endpoint = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
  s3_key_id   = cloudflare_api_token.files.id
  s3_secret   = sha256(cloudflare_api_token.files.value)
  # Through the tunnel, a worker sees Postgres and Redis on its own 127.0.0.1,
  # as the web server does.
  database_url = "postgresql+psycopg://subplz:${random_password.postgres.result}@127.0.0.1:5432/subplz"
  redis_url    = "redis://:${random_password.redis.result}@127.0.0.1:6379/0"
  known_hosts = (
    var.ssh_port == 22
    ? "${var.web_server_ip} ${trimspace(file(var.host_key_file))}"
    : "[${var.web_server_ip}]:${var.ssh_port} ${trimspace(file(var.host_key_file))}"
  )
  # boto3 1.36 and later send checksums that some S3 services refuse.
  storage_env = <<-EOT
    AWS_ACCESS_KEY_ID=${local.s3_key_id}
    AWS_SECRET_ACCESS_KEY=${local.s3_secret}
    AWS_ENDPOINT_URL_S3=${local.s3_endpoint}
    AWS_DEFAULT_REGION=auto
    AWS_REQUEST_CHECKSUM_CALCULATION=when_required
    AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
  EOT
}
