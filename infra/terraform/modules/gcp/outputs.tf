output "cluster_endpoint" {
  value = google_container_cluster.this.endpoint
}

output "network_id" {
  value = google_compute_network.this.id
}
