resource "google_firestore_database" "default" {
  project     = var.gcp_project
  name        = "(default)"
  location_id = "us-central1"
  type        = "FIRESTORE_NATIVE"

  lifecycle {
    prevent_destroy = true
  }
}
