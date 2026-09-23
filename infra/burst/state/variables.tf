variable "cloudflare_account_id" {
  description = "The Cloudflare account that holds the bucket (R2, right-hand column)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be the 32-character account ID."
  }
}

variable "r2_permission_group_ids" {
  description = "Ids of the R2 permission groups that read and write objects. Null: look them up by name."
  type        = list(string)
  default     = null
}

variable "bucket_name" {
  type    = string
  default = "subplz-files"
}

variable "bucket_location" {
  description = "R2 location hint. wnam: western North America, near the web server."
  type        = string
  default     = "wnam"
}

variable "web_server_ip" {
  description = "Public IPv4 address of the web server. Burst workers open their SSH tunnel to it."
  type        = string
  default     = "198.44.53.27"

  validation {
    condition     = can(cidrhost("${var.web_server_ip}/32", 0))
    error_message = "web_server_ip must be one IPv4 address."
  }
}

variable "ssh_port" {
  type    = number
  default = 22
}

variable "host_key_file" {
  description = "The web server's SSH host key. Workers accept this key only."
  type        = string
  default     = "/etc/ssh/ssh_host_ed25519_key.pub"
}
