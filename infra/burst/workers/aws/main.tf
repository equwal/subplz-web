# Burst workers on AWS: one spot instance for each entry of var.workers. The
# burst controller (python -m tools.burst) writes var.workers and applies this
# when the set changes; do not apply it by hand while burst mode is on.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66"
    }
  }
  backend "local" {}
}

# Reads AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and AWS_REGION. The controller
# sets them from SUBPLZ_BURST_AWS_* (see tools/burst/__main__.py).
provider "aws" {}

data "terraform_remote_state" "state" {
  backend = "local"
  config = {
    path = var.state_file
  }
}

locals {
  state = data.terraform_remote_state.state.outputs.worker
  # Graviton families (c7g, m6g ...) are arm64; the others are x86-64.
  arch = { for name, w in var.workers : name => can(regex("^[a-z]+[0-9]+g[a-z]*\\.", w.machine)) ? "arm64" : "amd64" }
}

data "aws_ami" "debian" {
  for_each    = toset(["amd64", "arm64"])
  most_recent = true
  owners      = ["136693071363"] # Debian
  filter {
    name   = "name"
    values = ["debian-12-${each.key}-*"]
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "zone" {
  for_each          = toset([for w in var.workers : w.zone])
  vpc_id            = data.aws_vpc.default.id
  availability_zone = each.key
  default_for_az    = true
}

# No inbound port: a worker only connects out (the SSH tunnel, the bucket, the
# package indexes).
resource "aws_security_group" "worker" {
  name        = "subplz-burst-worker"
  description = "subplz burst workers: outbound only"
  vpc_id      = data.aws_vpc.default.id

  # An empty list, not a missing one: Terraform then removes any inbound rule.
  ingress = []

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "worker" {
  for_each = var.workers

  ami                                  = data.aws_ami.debian[local.arch[each.key]].id
  instance_type                        = each.value.machine
  subnet_id                            = data.aws_subnet.zone[each.value.zone].id
  vpc_security_group_ids               = [aws_security_group.worker.id]
  associate_public_ip_address          = true
  instance_initiated_shutdown_behavior = "terminate" # the idle check powers off: the bill ends

  instance_market_options {
    market_type = "spot"
    spot_options {
      max_price                      = var.max_price
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  metadata_options {
    http_tokens = "required"
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

  tags = {
    Name = each.key
    Role = "subplz-burst-worker"
  }

  # A change of these would replace a machine, and kill the job on it. New
  # workers get the new values; the controller replaces old ones when idle.
  lifecycle {
    ignore_changes = [user_data, ami]
  }
}
