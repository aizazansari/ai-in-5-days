locals {
  required_apis = [
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "secretmanager.googleapis.com",
    "dlp.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "artifactregistry.googleapis.com",
  ]

  agent_iam_roles = [
    "roles/aiplatform.user",
    "roles/dlp.user",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
  ]
}

# 1. Enable Required Google Cloud APIs
resource "google_project_service" "enabled_apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# 2. Artifact Registry Repository for Agent Containers
resource "google_artifact_registry_repository" "agent_repo" {
  location      = var.region
  repository_id = "shopguard"
  description   = "Container registry for ShopGuard ADK E-Commerce Support Agent"
  format        = "DOCKER"

  depends_on = [google_project_service.enabled_apis]
}

# 3. Dedicated Least-Privilege Runtime Service Account
resource "google_service_account" "agent_runtime_sa" {
  account_id   = "shopguard-agent-sa"
  display_name = "ShopGuard ADK Runtime Service Account"
}

resource "google_project_iam_member" "agent_runtime_roles" {
  for_each = toset(local.agent_iam_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

# 4. Secure Secret Manager Resources (Zero Hardcoded Credentials)
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "shopguard-gemini-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret" "session_db_url" {
  secret_id = "shopguard-session-db-url"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_iam_member" "gemini_secret_access" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "session_db_secret_access" {
  secret_id = google_secret_manager_secret.session_db_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

# 5. Cloud Run v2 Service Hosting the ADK Multi-Agent Application
resource "google_cloud_run_v2_service" "shopguard_agent" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.agent_runtime_sa.email

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "FAST_ROUTER_MODEL"
        value = "gemini-2.5-flash"
      }
      env {
        name  = "REASONING_PLANNER_MODEL"
        value = "gemini-2.5-pro"
      }
      env {
        name  = "MEMORY_BACKEND"
        value = var.vertex_agent_engine_id != "" ? "vertex" : "local"
      }
      env {
        name  = "VERTEX_AGENT_ENGINE_ID"
        value = var.vertex_agent_engine_id
      }
      env {
        name  = "HIGH_VALUE_REFUND_THRESHOLD_USD"
        value = var.high_value_refund_threshold_usd
      }
      env {
        name  = "ENABLE_CLOUD_DLP"
        value = "true"
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "true"
      }
      env {
        name = "SESSION_DB_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.session_db_url.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.enabled_apis,
    google_secret_manager_secret_iam_member.session_db_secret_access,
  ]
}
