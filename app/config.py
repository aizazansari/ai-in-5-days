"""Configuration and Google Cloud Secret Manager integration.

Ensures zero hardcoded API keys or database credentials exist in source code.
In cloud deployments, secrets are fetched dynamically from Google Cloud Secret
Manager with a secure local environment variable fallback for offline testing.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from google.cloud import secretmanager
except ImportError:  # pragma: no cover - optional in minimal test envs
    secretmanager = None  # type: ignore[assignment]


def fetch_secret_from_gcp(
    secret_id: str,
    project_id: str | None = None,
    version_id: str = "latest",
    env_fallback_key: str | None = None,
) -> str | None:
    """Retrieve a secret payload from Google Cloud Secret Manager.

    Falls back cleanly to an environment variable during local development or CI
    runs when Google Cloud credentials or project configuration are absent.

    Args:
        secret_id: Name of the secret in Google Cloud Secret Manager.
        project_id: GCP project ID hosting the secret.
        version_id: Secret version (defaults to ``"latest"``).
        env_fallback_key: Optional environment variable name for local dev fallback.

    Returns:
        Resolved secret string, or ``None`` if unavailable in both sources.
    """
    resolved_project = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if resolved_project and secretmanager is not None:
        try:
            client = secretmanager.SecretManagerServiceClient()
            resource_name = (
                f"projects/{resolved_project}/secrets/{secret_id}/versions/{version_id}"
            )
            response = client.access_secret_version(request={"name": resource_name})
            payload = response.payload.data.decode("UTF-8").strip()
            if payload:
                return payload
        except Exception:
            # Fall through to local environment variable fallback if Secret Manager
            # is unreachable in local/offline environments.
            pass

    if env_fallback_key:
        fallback_val = os.getenv(env_fallback_key, "").strip()
        return fallback_val or None
    return None


@dataclass(frozen=True)
class AgentSettings:
    """Immutable runtime settings for the ShopGuard E-Commerce Support Agent."""

    app_name: str
    fast_router_model: str
    reasoning_planner_model: str
    memory_backend: str
    session_db_url: str
    ecommerce_store_db_path: str
    chroma_persist_dir: str
    google_cloud_project: str
    google_cloud_location: str
    vertex_agent_engine_id: str
    vertex_search_datastore_id: str
    high_value_refund_threshold_usd: float
    enable_cloud_dlp: bool
    enable_cloud_trace: bool
    log_level: str


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    """Load and cache runtime configuration, injecting secrets securely."""
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    gemini_secret_id = os.getenv("GEMINI_SECRET_ID", "shopguard-gemini-api-key")

    # Inject GOOGLE_API_KEY from Secret Manager when not using Vertex AI IAM auth
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"
    if not use_vertex and not os.getenv("GOOGLE_API_KEY"):
        resolved_key = fetch_secret_from_gcp(
            secret_id=gemini_secret_id,
            project_id=project_id,
            env_fallback_key="GOOGLE_API_KEY",
        )
        if resolved_key:
            os.environ["GOOGLE_API_KEY"] = resolved_key

    # Resolve persistent session database URL (Secret Manager or env fallback)
    default_sqlite_url = "sqlite+aiosqlite:///./data/ecommerce_sessions.db"
    session_db_url = (
        fetch_secret_from_gcp(
            secret_id=os.getenv("SESSION_DB_SECRET_ID", "shopguard-session-db-url"),
            project_id=project_id,
            env_fallback_key="SESSION_DB_URL",
        )
        or default_sqlite_url
    )

    store_db_path = os.getenv("ECOMMERCE_STORE_DB_PATH", "./data/ecommerce_store.db")
    chroma_dir = os.getenv("CHROMA_PERSIST_DIR", "./data/chroma_vector_store")

    # Ensure local persistent directories exist when running in local mode
    Path(store_db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(chroma_dir).mkdir(parents=True, exist_ok=True)

    return AgentSettings(
        app_name="shopguard_ecommerce_support",
        fast_router_model=os.getenv("FAST_ROUTER_MODEL", "gemini-2.5-flash"),
        reasoning_planner_model=os.getenv("REASONING_PLANNER_MODEL", "gemini-2.5-pro"),
        memory_backend=os.getenv("MEMORY_BACKEND", "local").lower(),
        session_db_url=session_db_url,
        ecommerce_store_db_path=store_db_path,
        chroma_persist_dir=chroma_dir,
        google_cloud_project=project_id,
        google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        vertex_agent_engine_id=os.getenv("VERTEX_AGENT_ENGINE_ID", "").strip(),
        vertex_search_datastore_id=os.getenv("VERTEX_SEARCH_DATASTORE_ID", "").strip(),
        high_value_refund_threshold_usd=float(
            os.getenv("HIGH_VALUE_REFUND_THRESHOLD_USD", "100.0")
        ),
        enable_cloud_dlp=os.getenv("ENABLE_CLOUD_DLP", "false").lower() == "true",
        enable_cloud_trace=os.getenv("ENABLE_CLOUD_TRACE", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
