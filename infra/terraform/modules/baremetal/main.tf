# Bare-metal target: hosts are pre-provisioned; this module renders an
# Ansible inventory + kubeadm bootstrap variables so the same Helm chart
# deploys onto any conformant cluster. No cloud resources are created.

terraform {
  required_providers {
    local = {
      source  = "hashicorp/local"
      version = ">= 2.4"
    }
  }
}

locals {
  name = "omniresolve-${var.environment}"

  inventory = templatefile("${path.module}/templates/inventory.ini.tftpl", {
    node_ips = var.node_ips
    ssh_user = var.ssh_user
  })
}

resource "local_file" "inventory" {
  filename        = "${path.root}/generated/${local.name}-inventory.ini"
  content         = local.inventory
  file_permission = "0640"
}
