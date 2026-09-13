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
  type    = string
  default = "23a6285cbf24ec717a94a3464f1256492dc8b33d"
}
