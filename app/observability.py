"""Structured JSON Logging and OpenTelemetry Distributed Tracing setup.

Provides:
1. Cloud-Logging-compatible structured JSON logs via ``python-json-logger`` with
   automatic PII scrubbing and OpenTelemetry trace/span correlation.
2. End-to-end distributed tracing via OpenTelemetry ``TracerProvider`` linking
   request -> agent -> LLM -> tool spans, with optional Cloud Trace export.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from .config import get_settings
from .pii import pii_redactor

try:
    from pythonjsonlogger import jsonlogger
except ImportError:  # pragma: no cover - fallback JSON formatter if missing
    jsonlogger = None  # type: ignore[assignment]

try:
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
except ImportError:  # pragma: no cover
    CloudTraceSpanExporter = None  # type: ignore[assignment,misc]

# In-memory exporter retained so unit/integration tests can assert span hierarchies
IN_MEMORY_SPAN_EXPORTER = InMemorySpanExporter()
_TELEMETRY_INITIALIZED = False


class PIIRedactingTraceFilter(logging.Filter):
    """Injects OpenTelemetry trace/span IDs and scrubs PII from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        span_ctx = span.get_span_context() if span else None
        if span_ctx and span_ctx.is_valid:
            record.trace_id = f"{span_ctx.trace_id:032x}"
            record.span_id = f"{span_ctx.span_id:016x}"
        else:
            record.trace_id = "0" * 32
            record.span_id = "0" * 16

        record.severity = record.levelname

        if isinstance(record.msg, str):
            record.msg = pii_redactor.scrub_text(record.msg)
        if record.args:
            record.args = pii_redactor.scrub_payload(record.args)

        # Scrub any structured extra fields attached to the LogRecord
        for attr in ("intent_payload", "outcome_payload", "tool_args", "metadata"):
            if hasattr(record, attr):
                setattr(record, attr, pii_redactor.scrub_payload(getattr(record, attr)))
        return True


class _FallbackJsonFormatter(logging.Formatter):
    """Minimal structured JSON formatter used only if python-json-logger isn't installed."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "severity": getattr(record, "severity", record.levelname),
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", "0" * 32),
            "span_id": getattr(record, "span_id", "0" * 16),
        }
        for extra_key in (
            "event_type",
            "event_phase",
            "agent_name",
            "tool_name",
            "session_id",
            "latency_ms",
            "intent_payload",
            "outcome_payload",
            "metadata",
        ):
            if hasattr(record, extra_key):
                payload[extra_key] = getattr(record, extra_key)
        return json.dumps(pii_redactor.scrub_payload(payload), default=str)


def setup_observability() -> tuple[logging.Logger, trace.Tracer]:
    """Configure structured JSON logging and OpenTelemetry TracerProvider once."""
    global _TELEMETRY_INITIALIZED
    settings = get_settings()

    if not _TELEMETRY_INITIALIZED:
        resource = Resource.create(
            {
                "service.name": settings.app_name,
                "service.version": "1.0.0",
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(SimpleSpanProcessor(IN_MEMORY_SPAN_EXPORTER))

        if (
            settings.enable_cloud_trace
            and settings.google_cloud_project
            and CloudTraceSpanExporter is not None
        ):
            cloud_exporter = CloudTraceSpanExporter(
                project_id=settings.google_cloud_project
            )
            provider.add_span_processor(BatchSpanProcessor(cloud_exporter))

        trace.set_tracer_provider(provider)
        _TELEMETRY_INITIALIZED = True

    logger = logging.getLogger("shopguard.agentops")
    logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        if jsonlogger is not None:
            formatter: logging.Formatter = jsonlogger.JsonFormatter(
                "%(asctime)s %(severity)s %(name)s %(message)s "
                "%(trace_id)s %(span_id)s"
            )
        else:
            formatter = _FallbackJsonFormatter()
        handler.setFormatter(formatter)
        handler.addFilter(PIIRedactingTraceFilter())
        logger.addHandler(handler)

    return logger, trace.get_tracer(settings.app_name)


structured_logger, tracer = setup_observability()


def log_structured_event(
    message: str,
    *,
    event_type: str,
    event_phase: str,
    agent_name: str | None = None,
    tool_name: str | None = None,
    session_id: str | None = None,
    latency_ms: float | None = None,
    intent_payload: dict[str, Any] | None = None,
    outcome_payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    level: int = logging.INFO,
) -> None:
    """Emit a rich, PII-redacted structured JSON log entry."""
    extra: dict[str, Any] = {
        "event_type": event_type,
        "event_phase": event_phase,
    }
    if agent_name is not None:
        extra["agent_name"] = agent_name
    if tool_name is not None:
        extra["tool_name"] = tool_name
    if session_id is not None:
        extra["session_id"] = session_id
    if latency_ms is not None:
        extra["latency_ms"] = round(latency_ms, 2)
    if intent_payload is not None:
        extra["intent_payload"] = pii_redactor.scrub_payload(intent_payload)
    if outcome_payload is not None:
        extra["outcome_payload"] = pii_redactor.scrub_payload(outcome_payload)
    if metadata is not None:
        extra["metadata"] = pii_redactor.scrub_payload(metadata)

    structured_logger.log(level, pii_redactor.scrub_text(message), extra=extra)
