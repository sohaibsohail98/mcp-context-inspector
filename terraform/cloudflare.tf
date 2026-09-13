data "cloudflare_zone" "ctxwindow" {
  zone_id = var.cloudflare_zone_id
}

resource "cloudflare_workers_custom_domain" "apex" {
  account_id = var.cloudflare_account_id
  zone_id    = data.cloudflare_zone.ctxwindow.zone_id
  zone_name  = var.cloudflare_zone
  hostname   = var.cloudflare_zone
  service    = "mcp-inspector"
}

resource "cloudflare_workers_custom_domain" "www" {
  account_id = var.cloudflare_account_id
  zone_id    = data.cloudflare_zone.ctxwindow.zone_id
  zone_name  = var.cloudflare_zone
  hostname   = "www.${var.cloudflare_zone}"
  service    = "mcp-inspector"
}
