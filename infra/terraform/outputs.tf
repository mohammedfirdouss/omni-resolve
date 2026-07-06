output "cluster_endpoint" {
  description = "Kubernetes API endpoint (or ansible inventory path for baremetal)."
  value = coalesce(
    try(module.aws[0].cluster_endpoint, null),
    try(module.gcp[0].cluster_endpoint, null),
    try(module.baremetal[0].inventory_path, null),
    "n/a"
  )
}

output "network_id" {
  description = "Provisioned network identifier."
  value = coalesce(
    try(module.aws[0].vpc_id, null),
    try(module.gcp[0].network_id, null),
    try(module.baremetal[0].network_id, null),
    "n/a"
  )
}
