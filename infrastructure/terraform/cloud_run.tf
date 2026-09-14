resource "google_cloud_run_v2_service" "mcp_context_inspector" {
  project  = var.gcp_project
  name     = "mcp-context-inspector"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.run.email
    timeout                          = "30s"
    max_instance_request_concurrency = 80

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.gcp_project}/${google_artifact_registry_repository.sre_platform.repository_id}/mcp-context-inspector:${var.image_tag}"

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = false
      }

      startup_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        period_seconds    = 10
        timeout_seconds   = 5
        failure_threshold = 3
      }

      env {
        name  = "STORAGE_BACKEND"
        value = "firestore"
      }
      env {
        name  = "DEMO_SEED_SRC"
        value = "/app/demo/metrics.db"
      }
      env {
        name  = "METRICS_DB_PATH"
        value = "/tmp/metrics.db"
      }
      env {
        name  = "CHAT_UI_ORIGIN"
        value = "https://sre-agent.sohaibsohail.workers.dev"
      }
      env {
        name  = "GOOGLE_OAUTH_CLIENT_ID"
        value = "1097847824883-6i3p5kjcfk27rolvcnk1jtlitjtp1nmm.apps.googleusercontent.com"
      }
      env {
        name  = "MCP_ALLOWED_HOSTS"
        value = "mcp-context-inspector-1097847824883.us-central1.run.app,mcp-context-inspector-bge5ndkzfq-uc.a.run.app"
      }
      env {
        name  = "PUBLIC_ORIGIN"
        value = "https://ctxwindow.uk"
      }
      env {
        name = "MCP_AUTH_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.mcp_auth_token.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      template[0].labels,
      template[0].annotations,
      client,
      client_version,
    ]
  }

  # All traffic follows the newest revision. There is deliberately no
  # second, pinned traffic target: the imported state carried a
  # "fix-verify" tag pointing at revision mcp-context-inspector-00036-jev
  # at 0%, left over from manual debugging. Pinning a revision by name
  # makes every future apply depend on that one revision still existing,
  # so the first time Cloud Run garbage-collects it the deploy breaks for
  # a tag nothing routes to. Removing it moves no production traffic (it
  # was already 0%); it only drops the fix-verify preview URL.
  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

data "google_cloud_run_v2_service" "web_chat_ui" {
  project  = var.gcp_project
  name     = "web-chat-ui"
  location = var.region
}
