variable "target" {
  description = "Deployment target: aws, gcp, or baremetal."
  type        = string

  validation {
    condition     = contains(["aws", "gcp", "baremetal"], var.target)
    error_message = "target must be one of: aws, gcp, baremetal."
  }
}

variable "environment" {
  description = "Environment name (dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "region" {
  description = "Cloud region (ignored for baremetal)."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR block for the platform network."
  type        = string
  default     = "10.42.0.0/16"
}

variable "kubernetes_version" {
  description = "Kubernetes control-plane version (>= 1.27 per Requirement 10.6)."
  type        = string
  default     = "1.29"

  validation {
    condition     = tonumber(split(".", var.kubernetes_version)[1]) >= 27
    error_message = "kubernetes_version must be 1.27 or later."
  }
}

variable "node_instance_type" {
  description = "Worker node instance/machine type."
  type        = string
  default     = "t3.xlarge"
}

variable "node_count" {
  description = "Number of worker nodes."
  type        = number
  default     = 3
}

variable "gcp_project_id" {
  description = "GCP project id (gcp target only)."
  type        = string
  default     = ""
}

variable "baremetal_node_ips" {
  description = "Pre-provisioned host IPs (baremetal target only)."
  type        = list(string)
  default     = []
}

variable "baremetal_ssh_user" {
  description = "SSH user for bare-metal hosts."
  type        = string
  default     = "ubuntu"
}
