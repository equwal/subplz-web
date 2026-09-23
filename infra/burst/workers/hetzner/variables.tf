variable "workers" {
  description = "The workers on Hetzner: server name -> app commit, server type, location. The burst controller writes this."
  type = map(object({
    ref     = string
    machine = string
    zone    = string
  }))
  default = {}

  validation {
    condition     = alltrue([for name in keys(var.workers) : can(regex("^subplz-burst-[0-9a-z-]{1,40}$", name))])
    error_message = "Each worker name must start with subplz-burst-: the controller finds its workers by that prefix."
  }
  validation {
    condition     = alltrue([for w in values(var.workers) : can(regex("^[0-9a-f]{40}$", w.ref))])
    error_message = "Each ref must be a full git commit hash."
  }
}


variable "threads" {
  description = "Whisper threads: all vCPUs of a 4-vCPU server type."
  type        = number
  default     = 4
}

variable "idle_checks" {
  description = "Idle checks (10 minutes each) before a machine powers itself off, if the controller is gone."
  type        = number
  default     = 9
}

variable "subplz_spec" {
  description = "The subplz package, pinned to the commit that the web server runs."
  type        = string
  default     = "subplz @ git+https://github.com/kanjieater/SubPlz.git@6be8b6fab185372b458bdde13c61f4da204dc6fc"
}

variable "state_file" {
  description = "Path of the state file of ../../state."
  type        = string
  default     = "/var/lib/subplz-burst/state.tfstate"
}
