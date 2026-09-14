data "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  project                   = var.gcp_project
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

resource "google_artifact_registry_repository_iam_member" "deploy_artifact_writer" {
  project    = var.gcp_project
  location   = google_artifact_registry_repository.sre_platform.location
  repository = google_artifact_registry_repository.sre_platform.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_cloud_run_v2_service_iam_member" "deploy_manages_run_service" {
  project  = var.gcp_project
  location = var.region
  name     = google_cloud_run_v2_service.mcp_context_inspector.name
  role     = "roles/run.admin"
  member   = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_secret_manager_secret_iam_member" "deploy_adds_secret_versions" {
  project   = var.gcp_project
  secret_id = google_secret_manager_secret.mcp_auth_token.secret_id
  role      = "roles/secretmanager.secretVersionManager"
  member    = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_storage_bucket_iam_member" "deploy_tfstate_admin" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_project_iam_member" "deploy_monitoring_editor" {
  project = var.gcp_project
  role    = "roles/monitoring.editor"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# Every permission here is a READ needed purely so `terraform plan` can
# refresh a resource github-deploy manages but does not hold a broad
# predefined role on. Nothing here grants data access or the ability to
# change an IAM policy.
#
# HOW TO EXTEND THIS SAFELY. When plan fails with a 403, add the exact
# permission the error names, never a predefined role that happens to
# contain it: an earlier revision of this file used
# roles/resourcemanager.projectIamAdmin to fix one read-only refresh
# error, which let github-deploy grant itself Owner.
#
# AND NOTE THE BOOTSTRAP. This role is applied to GCP only when the PR
# merges and deploy.yml runs `terraform apply`. Until that happens the
# LIVE role still has the old permission set, so a plan that needs a
# newly-added permission keeps failing on the PR. Adding one here is
# therefore two steps, not one: commit it, and have someone with
# project IAM admin run the matching
#   gcloud iam roles update deployTerraformReader --project=<project> \
#     --add-permissions=<permission>
# so the PR's own plan can go green before the merge that applies it.
# See infrastructure/terraform/README.md.
resource "google_project_iam_custom_role" "deploy_terraform_reader" {
  project     = var.gcp_project
  role_id     = "deployTerraformReader"
  title       = "Deploy Terraform reader"
  description = "Read-only permissions github-deploy needs to refresh Terraform state for resources it does not directly own or manage IAM on."
  permissions = [
    "resourcemanager.projects.getIamPolicy",
    "iam.serviceAccounts.get",
    "iam.serviceAccounts.getIamPolicy",
    "iam.workloadIdentityPools.get",
    "iam.workloadIdentityPools.getAttestationRules",
    "iam.roles.get",
    # NOT datastore.databases.get. On a FIRESTORE_NATIVE database that
    # permission authorises beginning and rolling back a transaction,
    # which is a data-plane write this account has no business holding,
    # and it does NOT authorise the Firestore Admin
    # projects.databases.get call the google_firestore_database refresh
    # actually makes. getMetadata is the one that does, and it is
    # metadata only: it cannot read a single document.
    "datastore.databases.getMetadata",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
    "artifactregistry.repositories.getIamPolicy",
    "secretmanager.secrets.get",
    "secretmanager.secrets.getIamPolicy",
    # roles/run.admin below is scoped to the Cloud Run *service* resource,
    # but polling a long-running update operation's status is a call
    # against the operation resource under the location, not the service,
    # so the scoped binding doesn't cover it. Without this, `terraform
    # apply` submits a Cloud Run update successfully but then 403s trying
    # to confirm it finished, even though the update itself goes through.
    "run.operations.get",
  ]
}

resource "google_project_iam_member" "deploy_terraform_reader" {
  project = var.gcp_project
  role    = google_project_iam_custom_role.deploy_terraform_reader.id
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_cloud_run_v2_service_iam_member" "deploy_views_web_chat_ui" {
  project  = var.gcp_project
  location = var.region
  name     = data.google_cloud_run_v2_service.web_chat_ui.name
  role     = "roles/run.viewer"
  member   = "serviceAccount:${google_service_account.deploy.email}"
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
