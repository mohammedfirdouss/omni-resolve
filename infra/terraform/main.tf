# OmniResolve root module — single invocation for every deployment target.
# Select the target with -var target=aws|gcp|baremetal and a matching
# -var-file (see environments/*.tfvars.example). No module source changes
# are required per environment (Requirement 10.3/10.4).

terraform {
  required_version = ">= 1.6"
}

module "aws" {
  count  = var.target == "aws" ? 1 : 0
  source = "./modules/aws"

  environment        = var.environment
  region             = var.region
  vpc_cidr           = var.vpc_cidr
  kubernetes_version = var.kubernetes_version
  node_instance_type = var.node_instance_type
  node_count         = var.node_count
}

module "gcp" {
  count  = var.target == "gcp" ? 1 : 0
  source = "./modules/gcp"

  environment        = var.environment
  project_id         = var.gcp_project_id
  region             = var.region
  vpc_cidr           = var.vpc_cidr
  kubernetes_version = var.kubernetes_version
  node_machine_type  = var.node_instance_type
  node_count         = var.node_count
}

module "baremetal" {
  count  = var.target == "baremetal" ? 1 : 0
  source = "./modules/baremetal"

  environment = var.environment
  node_ips    = var.baremetal_node_ips
  ssh_user    = var.baremetal_ssh_user
}
