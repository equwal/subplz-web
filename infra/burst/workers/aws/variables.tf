variable "workers" {
  description = "The workers on AWS: machine name -> app commit, instance type, availability zone. The burst controller writes this."
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

variable "max_price" {
  description = "Highest spot price per hour, in USD. Above it AWS does not start the machine."
  type        = string
  default     = "0.0625"
}

variable "threads" {
  description = "Whisper threads: all vCPUs of an xlarge instance."
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
