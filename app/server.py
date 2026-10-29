"""Production FastAPI Server Entrypoint for Cloud Run & Local Deployment.

Configures ADK's ``get_fast_api_app`` with:
- Persistent ``session_service_uri`` (SQLite locally or Cloud SQL / Vertex Agent Engine in prod)
- Optional Vertex AI Agent Engine Memory Bank URI
- Google Cloud Trace & OpenTelemetry integration
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
import uvicorn

from .config import get_settings
from .observability import log_structured_event, setup_observability

setup_observability()
settings = get_settings()

AGENTS_DIR = str(Path(__file__).resolve().parent.parent)


def create_fastapi_server() -> FastAPI:
    """Build the FastAPI server wired to persistent session & memory backends."""
    if (
        settings.memory_backend == "vertex"
        and settings.google_cloud_project
        and settings.vertex_agent_engine_id
    ):
        session_uri = f"agentengine://{settings.vertex_agent_engine_id}"
        memory_uri: str | None = f"agentengine://{settings.vertex_agent_engine_id}"
    else:
        session_uri = settings.session_db_url
        memory_uri = None

    enable_web_ui = os.getenv("SERVE_WEB_INTERFACE", "true").lower() == "true"
    fastapi_app: FastAPI = get_fast_api_app(
        agents_dir=AGENTS_DIR,
        session_service_uri=session_uri,
        memory_service_uri=memory_uri,
        web=enable_web_ui,
        trace_to_cloud=settings.enable_cloud_trace,
        otel_to_cloud=settings.enable_cloud_trace,
    )

    @fastapi_app.get("/healthz", tags=["ops"])
    async def healthcheck() -> dict[str, str]:
        return {
            "status": "healthy",
            "app": settings.app_name,
            "memory_backend": settings.memory_backend,
        }

    log_structured_event(
        "Initialized ShopGuard ADK FastAPI Server",
        event_type="SERVER_STARTUP",
        event_phase="OUTCOME",
        outcome_payload={
            "session_service_uri": session_uri,
            "memory_backend": settings.memory_backend,
            "web_ui_enabled": enable_web_ui,
        },
    )
    return fastapi_app


fastapi_app = create_fastapi_server()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.server:fastapi_app", host="0.0.0.0", port=port, reload=False)
