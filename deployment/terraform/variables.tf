variable "project_id" {
  description = "Google Cloud Project ID hosting the ShopGuard ADK Support Agent."
  type        = string
}

variable "region" {
  description = "Primary Google Cloud region for Cloud Run and Vertex AI."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name for the ADK agent API/Web server."
  type        = string
  default     = "shopguard-ecommerce-agent"
}

variable "container_image" {
  description = "Container image URI built and pushed to Artifact Registry."
  type        = string
  default     = "us-central1-docker.pkg.dev/placeholder-project/shopguard/agent:latest"
}

variable "vertex_agent_engine_id" {
  description = "Optional Vertex AI Agent Engine resource ID for managed Sessions & Memory Bank."
  type        = string
  default     = ""
}

variable "high_value_refund_threshold_usd" {
  description = "Dollar threshold above which Human-in-the-Loop supervisor approval is required."
  type        = string
  default     = "100.0"
}
