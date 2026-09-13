resource "google_secret_manager_secret" "mcp_auth_token" {
  project   = var.gcp_project
  secret_id = "mcp-auth-token"

  replication {
    auto {}
  }
}
