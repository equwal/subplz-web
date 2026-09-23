# terraform test with a mock provider: no account and no cost.

mock_provider "hcloud" {}

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
    "subplz-burst-1790000000-0" = { ref = "0123456789abcdef0123456789abcdef01234567", machine = "cx33", zone = "fsn1" }
  }
}

run "one_server_for_each_worker_behind_a_closed_firewall" {
  command = plan

  assert {
    condition = alltrue([for n, s in hcloud_server.worker :
      s.server_type == "cx33" && s.location == "fsn1" && s.image == "debian-12"
    ])
    error_message = "Each worker must be a Debian 12 server of its type, in its location."
  }
  assert {
    condition     = length(hcloud_firewall.worker.rule) == 0
    error_message = "The firewall must have no inbound rule, so that it drops all inbound traffic."
  }
}

run "the_cloud_config_names_the_host_and_opens_the_tunnel" {
  command = plan

  assert {
    condition = alltrue([for n, s in hcloud_server.worker :
      yamldecode(s.user_data).hostname == n
      && strcontains(s.user_data, "subplz-tunnel@198.44.53.27")
      && strcontains(s.user_data, "SUBPLZ_WEB_WORKER_QUEUES=paid\n")
    ])
    error_message = "The server must name itself, open the tunnel, and take paid jobs."
  }
}

run "a_short_ref_is_refused" {
  command = plan

  variables {
    workers = { "subplz-burst-1-0" = { ref = "0123abc", machine = "cx33", zone = "fsn1" } }
  }

  expect_failures = [var.workers]
}
