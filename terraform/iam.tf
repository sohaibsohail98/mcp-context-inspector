data "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  project                   = var.gcp_project
}

data "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = "github-pool"
  workload_identity_pool_provider_id = "github-provider"
  project                            = var.gcp_project
}

resource "google_service_account" "run" {
  project      = var.gcp_project
  account_id   = "mcp-inspector-run"
  display_name = "mcp-context-inspector Cloud Run SA"
}

resource "google_project_iam_member" "run_datastore_user" {
  project = var.gcp_project
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.run.email}"
}

resource "google_service_account" "deploy" {
  project      = var.gcp_project
  account_id   = "github-deploy"
  display_name = "GitHub Actions deploy (Cloud Run + Artifact Registry)"
}

resource "google_project_iam_member" "deploy_artifact_writer" {
  project = var.gcp_project
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_project_iam_member" "deploy_run_developer" {
  project = var.gcp_project
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_cloud_run_v2_service_iam_member" "deploy_manages_run_iam" {
  project  = var.gcp_project
  location = var.region
  name     = google_cloud_run_v2_service.mcp_context_inspector.name
  role     = "roles/run.admin"
  member   = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_secret_manager_secret_iam_member" "deploy_manages_secret_iam" {
  project   = var.gcp_project
  secret_id = google_secret_manager_secret.mcp_auth_token.secret_id
  role      = "roles/secretmanager.admin"
  member    = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_storage_bucket_iam_member" "deploy_tfstate_admin" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_service_account_iam_member" "deploy_can_actas_run" {
  service_account_id = google_service_account.run.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_service_account_iam_member" "deploy_workload_identity_binding" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${data.google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

resource "google_secret_manager_secret_iam_member" "run_reads_auth_token" {
  project   = var.gcp_project
  secret_id = google_secret_manager_secret.mcp_auth_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.gcp_project
  location = var.region
  name     = google_cloud_run_v2_service.mcp_context_inspector.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
