resource "google_artifact_registry_repository" "sre_platform" {
  project       = var.gcp_project
  location      = var.region
  repository_id = "sre-platform"
  format        = "DOCKER"
  description   = "mcp-context-inspector and sre-investigation-agent chat UI images"

  cleanup_policies {
    id     = "keep-recent-inspector"
    action = "KEEP"
    most_recent_versions {
      package_name_prefixes = ["mcp-context-inspector"]
      keep_count            = 3
    }
  }

  cleanup_policies {
    id     = "keep-recent-web-chat-ui"
    action = "KEEP"
    most_recent_versions {
      package_name_prefixes = ["web-chat-ui"]
      keep_count            = 3
    }
  }

  cleanup_policies {
    id     = "delete-rest"
    action = "DELETE"
    condition {
      tag_state = "ANY"
    }
  }
}
