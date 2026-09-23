# terraform test with mock providers: no account, no cost. Only plans run:
# an apply would run setup-web-server.sh on this machine.

mock_provider "cloudflare" {}

mock_provider "random" {}
mock_provider "tls" {}

# Values that exist only after apply, known during plan for the tests.
override_resource {
  target          = random_password.postgres
  override_during = plan
  values          = { result = "pgpw" }
}

override_resource {
  target          = random_password.redis
  override_during = plan
  values          = { result = "redispw" }
}

override_resource {
  target          = cloudflare_api_token.files
  override_during = plan
  values          = { id = "token-id", value = "token-value" }
}

variables {
  cloudflare_account_id = "0123456789abcdef0123456789abcdef"
  host_key_file         = "tests/host_key.pub"
  # The test framework cannot mock the nested result of the permission lookup.
  r2_permission_group_ids = ["read-group", "write-group"]
}

run "the_token_can_touch_the_bucket_and_nothing_else" {
  command = plan

  assert {
    condition = jsondecode(cloudflare_api_token.files.policies[0].resources) == {
      "com.cloudflare.edge.r2.bucket.0123456789abcdef0123456789abcdef_default_subplz-files" = "*"
    }
    error_message = "The app token must be scoped to the one bucket."
  }
  assert {
    condition     = [for g in cloudflare_api_token.files.policies[0].permission_groups : g.id] == ["read-group", "write-group"]
    error_message = "The app token must read and write objects, with no other permission."
  }
  assert {
    condition     = cloudflare_r2_bucket.files.location == "wnam"
    error_message = "The bucket must be near the web server (western North America)."
  }
}

run "workers_accept_only_the_web_server_host_key" {
  command = plan

  assert {
    condition     = startswith(local.known_hosts, "198.44.53.27 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKey")
    error_message = "known_hosts must pin the web server's host key."
  }
}

run "a_port_other_than_22_is_written_the_ssh_way" {
  command = plan

  variables {
    ssh_port = 2222
  }

  assert {
    condition     = startswith(local.known_hosts, "[198.44.53.27]:2222 ssh-ed25519 ")
    error_message = "known_hosts needs [host]:port for a port other than 22."
  }
}

run "the_databases_are_reached_on_127_0_0_1" {
  command = plan

  assert {
    condition     = startswith(local.database_url, "postgresql+psycopg://subplz:") && endswith(local.database_url, "@127.0.0.1:5432/subplz")
    error_message = "Postgres must be reached through the tunnel on 127.0.0.1."
  }
  assert {
    condition     = startswith(local.redis_url, "redis://:") && endswith(local.redis_url, "@127.0.0.1:6379/0")
    error_message = "Redis must be reached through the tunnel on 127.0.0.1."
  }
  assert {
    condition     = local.database_url == "postgresql+psycopg://subplz:pgpw@127.0.0.1:5432/subplz" && local.redis_url == "redis://:redispw@127.0.0.1:6379/0"
    error_message = "The URLs must carry the generated passwords."
  }
  assert {
    condition     = strcontains(local.storage_env, "AWS_ACCESS_KEY_ID=token-id\n") && strcontains(local.storage_env, "AWS_SECRET_ACCESS_KEY=${sha256("token-value")}\n")
    error_message = "R2 takes the token id as the key id, and the SHA-256 of the token value as the secret."
  }
  assert {
    condition     = strcontains(local.storage_env, "AWS_ENDPOINT_URL_S3=https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com\n")
    error_message = "The storage endpoint must be the account's R2 endpoint."
  }
}

run "a_bad_account_id_is_refused" {
  command = plan

  variables {
    cloudflare_account_id = "not-an-id"
  }

  expect_failures = [var.cloudflare_account_id]
}
