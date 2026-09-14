variable "gcp_project" {
  type        = string
  description = "GCP project hosting ctxwindow's infra. Also hosts two unrelated services (billing-killswitch, web-chat-ui); this config must never grant project-wide access that reaches them."
}

variable "region" {
  type        = string
  description = "GCP region for the Cloud Run service and Artifact Registry repository."
}

variable "github_repo" {
  type        = string
  description = "This repo as GitHub Actions' OIDC token reports it (owner/repo), used to scope the workload identity binding to exactly this repo."
}

variable "image_tag" {
  type        = string
  description = "Container image tag to deploy. No default: CI always passes the commit SHA explicitly, and a hardcoded default here would let a bare local apply silently roll production back to an old image."
}

variable "alert_email" {
  type        = string
  description = "Address the uptime-check failure alert notifies. Not a secret, but kept out of terraform.tfvars anyway since it's a personal address rather than infra config; pass it explicitly per environment instead."
}
