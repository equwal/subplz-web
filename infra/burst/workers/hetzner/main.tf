# Burst workers on Hetzner Cloud: one server for each entry of var.workers. The
# burst controller (python -m tools.burst) writes var.workers and applies this
# when the set changes; do not apply it by hand while burst mode is on.

terraform {
  required_version = ">= 1.9"
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.69"
    }
  }
  backend "local" {}
}

# Reads HCLOUD_TOKEN. Keep the burst servers in a Hetzner project of their own.
provider "hcloud" {}

data "terraform_remote_state" "state" {
  backend = "local"
  config = {
    path = var.state_file
  }
}

locals {
  state = data.terraform_remote_state.state.outputs.worker
}

# No inbound rule: Hetzner then drops all inbound traffic. A worker only
# connects out (the SSH tunnel, the bucket, the package indexes).
resource "hcloud_firewall" "worker" {
  name = "subplz-burst-worker"
}

resource "hcloud_server" "worker" {
  for_each = var.workers

  name         = each.key
  server_type  = each.value.machine
  location     = each.value.zone
  image        = "debian-12"
  firewall_ids = [hcloud_firewall.worker.id]
  labels = {
    role = "subplz-burst-worker"
  }

  user_data = templatefile("${path.module}/../../cloud-init.yaml.tftpl", {
    name               = each.key
    ref                = each.value.ref
    subplz_spec        = var.subplz_spec
    threads            = var.threads
    idle_checks        = var.idle_checks
    constraints        = file("${path.module}/../../worker-constraints.txt")
    web_server_ip      = local.state.web_server_ip
    ssh_port           = local.state.ssh_port
    known_hosts        = local.state.known_hosts
    tunnel_private_key = local.state.tunnel_private_key
    database_url       = local.state.database_url
    redis_url          = local.state.redis_url
    bucket             = local.state.bucket
    s3_prefix          = local.state.s3_prefix
    storage_env        = local.state.storage_env
  })

  # A change of these would replace a server, and kill the job on it. New
  # workers get the new values; the controller replaces old ones when idle.
  lifecycle {
    ignore_changes = [user_data, image]
  }
}
