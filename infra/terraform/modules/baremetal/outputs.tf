output "inventory_path" {
  value = local_file.inventory.filename
}

output "network_id" {
  value = "preprovisioned"
}
