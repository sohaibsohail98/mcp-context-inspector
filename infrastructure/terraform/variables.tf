variable "gcp_project" {
  type    = string
  default = "modular-bucksaw-506000-k0"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "github_repo" {
  type    = string
  default = "sohaibsohail98/mcp-context-inspector"
}

variable "cloudflare_account_id" {
  type    = string
  default = "88a7917ac3e6f4eaca19437938ca5a2c"
}

variable "cloudflare_zone" {
  type    = string
  default = "ctxwindow.uk"
}

variable "cloudflare_zone_id" {
  type = string
}

variable "image_tag" {
  type        = string
  description = "Container image tag to deploy. No default on purpose: CI always passes the commit SHA explicitly, and a stale hardcoded default here would silently roll production back to an old image on any apply that forgets -var."
}
