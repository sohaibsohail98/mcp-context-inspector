output "cloud_run_url" {
  value = google_cloud_run_v2_service.mcp_context_inspector.uri
}

output "run_service_account_email" {
  value = google_service_account.run.email
}

output "deploy_service_account_email" {
  value = google_service_account.deploy.email
}
