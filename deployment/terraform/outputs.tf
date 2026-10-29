output "cloud_run_service_url" {
  description = "HTTPS endpoint URL of the deployed ShopGuard ADK Cloud Run service."
  value       = google_cloud_run_v2_service.shopguard_agent.uri
}

output "runtime_service_account_email" {
  description = "Least-privilege service account attached to the ADK Cloud Run service."
  value       = google_service_account.agent_runtime_sa.email
}

output "artifact_registry_repository" {
  description = "Docker repository URI for pushing agent container images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agent_repo.repository_id}"
}
