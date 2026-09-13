resource "google_storage_bucket" "tfstate_logs" {
  #checkov:skip=CKV_GCP_62:this bucket IS the log sink, logging itself is circular
  #checkov:skip=CKV_GCP_78:pure log sink with a 90-day TTL, versioning adds no value
  project                     = var.gcp_project
  name                        = "${var.gcp_project}-tfstate-access-logs"
  location                    = "US-CENTRAL1"
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket" "tfstate" {
  project                     = var.gcp_project
  name                        = "${var.gcp_project}-tfstate"
  location                    = "US-CENTRAL1"
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  logging {
    log_bucket = google_storage_bucket.tfstate_logs.name
  }

  lifecycle_rule {
    condition {
      age        = 30
      with_state = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }
}
