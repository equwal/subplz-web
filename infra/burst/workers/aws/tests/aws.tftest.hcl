# terraform test with a mock provider: no account and no cost.

mock_provider "aws" {}

override_data {
  target = data.terraform_remote_state.state
  values = {
    outputs = {
      worker = {
        web_server_ip      = "198.44.53.27"
        ssh_port           = 22
        known_hosts        = "198.44.53.27 ssh-ed25519 AAAAhostkey"
        tunnel_private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nTUNNELKEY\n-----END OPENSSH PRIVATE KEY-----\n"
        database_url       = "postgresql+psycopg://subplz:pgpw@127.0.0.1:5432/subplz"
        redis_url          = "redis://:redispw@127.0.0.1:6379/0"
        bucket             = "subplz-files"
        s3_prefix          = "jobs/"
        storage_env        = "AWS_ACCESS_KEY_ID=kid\nAWS_SECRET_ACCESS_KEY=secret\nAWS_ENDPOINT_URL_S3=https://acct.r2.cloudflarestorage.com\nAWS_DEFAULT_REGION=auto\n"
      }
    }
  }
}

variables {
  workers = {
    "subplz-burst-1790000000-0" = { ref = "0123456789abcdef0123456789abcdef01234567", machine = "c6a.xlarge", zone = "us-west-2a" }
    "subplz-burst-1790000000-1" = { ref = "89abcdef0123456789abcdef0123456789abcdef", machine = "c7g.xlarge", zone = "us-west-2b" }
  }
}

run "one_spot_instance_for_each_worker" {
  command = plan

  assert {
    condition     = length(aws_instance.worker) == 2
    error_message = "Expected one instance for each worker."
  }
  assert {
    condition = alltrue([for n, i in aws_instance.worker :
      i.instance_type == var.workers[n].machine
      && i.instance_market_options[0].market_type == "spot"
      && i.instance_market_options[0].spot_options[0].max_price == "0.0625"
      && i.instance_initiated_shutdown_behavior == "terminate"
    ])
    error_message = "Each worker must be a spot instance of its type, with a price limit, that ends when it powers off."
  }
  assert {
    condition     = local.arch["subplz-burst-1790000000-0"] == "amd64" && local.arch["subplz-burst-1790000000-1"] == "arm64"
    error_message = "A Graviton type must get the arm64 image, the others the amd64 image."
  }
  assert {
    condition     = length(aws_security_group.worker.ingress) == 0
    error_message = "A worker must take no inbound connection."
  }
}

run "the_cloud_config_names_the_host_and_opens_the_tunnel" {
  command = plan

  assert {
    condition     = alltrue([for n, i in aws_instance.worker : yamldecode(i.user_data).hostname == n])
    error_message = "The host name must be the machine name: the controller matches rq workers by host name."
  }
  assert {
    condition = alltrue([for n, i in aws_instance.worker :
      strcontains(i.user_data, "releases/${var.workers[n].ref}.tar.gz")
      && strcontains(i.user_data, "SUBPLZ_WEB_WORKER_QUEUES=paid\n")
      && strcontains(i.user_data, "-L 127.0.0.1:5432:127.0.0.1:5432 -L 127.0.0.1:6379:127.0.0.1:6379 subplz-tunnel@198.44.53.27")
      && strcontains(i.user_data, "AWS_ENDPOINT_URL_S3=https://acct.r2.cloudflarestorage.com\n")
    ])
    error_message = "Each worker must run its own commit, take paid jobs, reach the databases through the tunnel, and use the bucket."
  }
  assert {
    condition = alltrue([for i in aws_instance.worker :
      contains([for f in yamldecode(i.user_data).write_files : f.content],
      "-----BEGIN OPENSSH PRIVATE KEY-----\nTUNNELKEY\n-----END OPENSSH PRIVATE KEY-----\n")
    ])
    error_message = "The tunnel key must be written whole."
  }
  assert {
    condition = alltrue([for i in aws_instance.worker :
      strcontains(one([for f in yamldecode(i.user_data).write_files : f.content if f.path == "/opt/subplz-install/idle-check.sh"]),
    "-ge 9 ]")])
    error_message = "The idle check must power off after var.idle_checks idle checks."
  }
}

run "no_workers_means_no_instances" {
  command = plan

  variables {
    workers = {}
  }

  assert {
    condition     = length(aws_instance.worker) == 0
    error_message = "An empty set must make no instances."
  }
}

run "a_name_without_the_prefix_is_refused" {
  command = plan

  variables {
    workers = { "web-1" = { ref = "0123456789abcdef0123456789abcdef01234567", machine = "c6a.xlarge", zone = "us-west-2a" } }
  }

  expect_failures = [var.workers]
}
