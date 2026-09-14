resource "google_monitoring_notification_channel" "owner_email" {
  project      = var.gcp_project
  display_name = "ctxwindow owner"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

resource "google_monitoring_uptime_check_config" "health" {
  project      = var.gcp_project
  display_name = "mcp-context-inspector /health"
  timeout      = "10s"
  period       = "60s"

  http_check {
    path         = "/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.gcp_project
      host       = "ctxwindow.uk"
    }
  }
}

resource "google_monitoring_alert_policy" "health_check_failing" {
  project      = var.gcp_project
  display_name = "ctxwindow.uk health check failing"
  combiner     = "OR"

  conditions {
    display_name = "Uptime check failure"
    condition_threshold {
      filter          = "resource.type = \"uptime_url\" AND metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.labels.check_id = \"${google_monitoring_uptime_check_config.health.uptime_check_id}\""
      comparison      = "COMPARISON_LT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_FRACTION_TRUE"
        cross_series_reducer = "REDUCE_MEAN"
        group_by_fields      = ["resource.label.host"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.owner_email.id]

  alert_strategy {
    auto_close = "1800s"
  }
}
