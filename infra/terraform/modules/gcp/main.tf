# GCP target: VPC + subnet + GKE cluster for the OmniResolve platform.
# Only base compute and network resources — data services run in-cluster
# via the Helm chart (Requirement 10.5).

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  name = "omniresolve-${var.environment}"
}

resource "google_compute_network" "this" {
  name                    = local.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "this" {
  name          = "${local.name}-nodes"
  network       = google_compute_network.this.id
  region        = var.region
  ip_cidr_range = var.vpc_cidr
}

resource "google_container_cluster" "this" {
  name     = local.name
  location = var.region

  network    = google_compute_network.this.id
  subnetwork = google_compute_subnetwork.this.id

  min_master_version       = var.kubernetes_version
  remove_default_node_pool = true
  initial_node_count       = 1

  deletion_protection = false
}

resource "google_container_node_pool" "default" {
  name     = "${local.name}-default"
  cluster  = google_container_cluster.this.id
  location = var.region

  node_count = var.node_count

  node_config {
    machine_type = var.node_machine_type

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }
}
