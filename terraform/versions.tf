terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.2"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.25"
    }
  }

  backend "gcs" {
    bucket = "modular-bucksaw-506000-k0-tfstate"
    prefix = "mcp-context-inspector"
  }
}

provider "google" {
  project = var.gcp_project
  region  = var.region
}

provider "cloudflare" {}
