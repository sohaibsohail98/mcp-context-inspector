output "cloud_run_url" {
  description = "Direct Cloud Run URL for mcp-context-inspector, used by the deploy workflow's smoke test."
  value       = google_cloud_run_v2_service.mcp_context_inspector.uri
}

output "run_service_account_email" {
  description = "Service account the Cloud Run service runs as."
  value       = google_service_account.run.email
}

output "deploy_service_account_email" {
  description = "Service account GitHub Actions impersonates via Workload Identity Federation to deploy."
  value       = google_service_account.deploy.email
}

output "sibling_web_chat_ui_url" {
  description = "URL of web-chat-ui, a Cloud Run service in the same GCP project owned by sre-investigation-agent's own Terraform. Read-only reference: this config has no permissions on it."
  value       = data.google_cloud_run_v2_service.web_chat_ui.uri
}
