"""ADK Lifecycle Callbacks for Intent-vs-Outcome Logging, OpenTelemetry Tracing, and Async Memory.

Uses ADK's exact keyword parameter contracts to capture:
- ``INTENT`` events *before* agent, LLM, and tool execution (with active span start).
- ``OUTCOME`` events *after* execution (recording status, latency_ms, and scrubbed outputs).
- Guided fallback recovery in ``on_tool_error_callback`` if an unexpected error ever arises.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from opentelemetry.trace import Status, StatusCode

from .memory import async_memory_consolidator
from .observability import log_structured_event, tracer
from .pii import pii_redactor
from .schemas import ToolErrorRecoveryPayload

# In-process span & timing registries keyed by invocation/call IDs
_ACTIVE_SPANS: dict[str, Any] = {}
_START_TIMES: dict[str, float] = {}


def _session_id_from_context(ctx: Any) -> str:
    session = getattr(ctx, "session", None)
    if session is not None and getattr(session, "id", None):
        return str(session.id)
    inv = getattr(ctx, "_invocation_context", None)
    if inv is not None and getattr(inv, "session", None) is not None:
        return str(inv.session.id)
    return "unknown-session"


def before_agent_callback(callback_context: CallbackContext) -> None:
    """Record agent INTENT and open a parent OpenTelemetry span for this agent run."""
    agent_name = callback_context.agent_name
    session_id = _session_id_from_context(callback_context)
    key = f"agent:{session_id}:{agent_name}"

    turn_count = int(callback_context.state.get("turn_count", 0)) + 1
    callback_context.state["turn_count"] = turn_count

    span = tracer.start_span(f"agent.{agent_name}")
    span.set_attribute("agent.name", agent_name)
    span.set_attribute("session.id", session_id)
    span.set_attribute("conversation.turn", turn_count)
    _ACTIVE_SPANS[key] = span
    _START_TIMES[key] = time.perf_counter()

    log_structured_event(
        f"Agent '{agent_name}' starting execution",
        event_type="AGENT_EXECUTION",
        event_phase="INTENT",
        agent_name=agent_name,
        session_id=session_id,
        intent_payload={
            "turn_count": turn_count,
            "active_order_id": callback_context.state.get("active_order_id"),
            "active_customer_id": callback_context.state.get("active_customer_id"),
        },
    )


def after_agent_callback(callback_context: CallbackContext) -> None:
    """Record agent OUTCOME, close OpenTelemetry span, and trigger async memory sync."""
    agent_name = callback_context.agent_name
    session_id = _session_id_from_context(callback_context)
    key = f"agent:{session_id}:{agent_name}"

    started = _START_TIMES.pop(key, None)
    latency_ms = (time.perf_counter() - started) * 1000.0 if started else 0.0
    span = _ACTIVE_SPANS.pop(key, None)
    if span is not None:
        span.set_attribute("agent.latency_ms", round(latency_ms, 2))
        span.set_status(Status(StatusCode.OK))
        span.end()

    customer_id = callback_context.state.get("active_customer_id")
    active_order_id = callback_context.state.get("active_order_id")

    # Schedule non-blocking session consolidation when customer context is present
    if customer_id and agent_name == "ecommerce_support_coordinator":
        session_obj = getattr(
            getattr(callback_context, "_invocation_context", None), "session", None
        )
        async_memory_consolidator.schedule_consolidation(
            customer_id=str(customer_id),
            interaction_summary=(
                f"Completed support turn #{callback_context.state.get('turn_count', 1)} "
                f"for customer {customer_id} (active_order={active_order_id})."
            ),
            session=session_obj,
        )

    log_structured_event(
        f"Agent '{agent_name}' finished execution",
        event_type="AGENT_EXECUTION",
        event_phase="OUTCOME",
        agent_name=agent_name,
        session_id=session_id,
        latency_ms=latency_ms,
        outcome_payload={
            "status": "completed",
            "active_order_id": active_order_id,
            "last_refund_id": callback_context.state.get("last_refund_id"),
            "last_escalation_ticket_id": callback_context.state.get(
                "last_escalation_ticket_id"
            ),
        },
    )


def before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> LlmResponse | None:
    """Record LLM generation INTENT and start an OpenTelemetry model span."""
    agent_name = callback_context.agent_name
    session_id = _session_id_from_context(callback_context)
    model_name = str(getattr(llm_request, "model", None) or "gemini")
    key = f"model:{session_id}:{agent_name}"

    contents = getattr(llm_request, "contents", []) or []
    span = tracer.start_span(f"llm.{model_name}")
    span.set_attribute("gen_ai.system", "gcp.gemini")
    span.set_attribute("gen_ai.request.model", model_name)
    span.set_attribute("agent.name", agent_name)
    span.set_attribute("llm.contents_count", len(contents))
    _ACTIVE_SPANS[key] = span
    _START_TIMES[key] = time.perf_counter()

    log_structured_event(
        f"Dispatching LLM request to '{model_name}'",
        event_type="LLM_GENERATION",
        event_phase="INTENT",
        agent_name=agent_name,
        session_id=session_id,
        intent_payload={
            "model": model_name,
            "message_turn_count": len(contents),
            "tools_available": list((getattr(llm_request, "tools_dict", None) or {}).keys()),
        },
    )
    return None


def after_model_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse | None:
    """Record LLM generation OUTCOME and close the OpenTelemetry model span."""
    agent_name = callback_context.agent_name
    session_id = _session_id_from_context(callback_context)
    key = f"model:{session_id}:{agent_name}"

    started = _START_TIMES.pop(key, None)
    latency_ms = (time.perf_counter() - started) * 1000.0 if started else 0.0

    part_types: list[str] = []
    content = getattr(llm_response, "content", None)
    for part in getattr(content, "parts", None) or []:
        if getattr(part, "function_call", None):
            part_types.append(f"function_call:{part.function_call.name}")
        elif getattr(part, "text", None):
            part_types.append("text")

    span = _ACTIVE_SPANS.pop(key, None)
    if span is not None:
        span.set_attribute("llm.latency_ms", round(latency_ms, 2))
        span.set_attribute("llm.response_parts", ",".join(part_types))
        span.set_status(Status(StatusCode.OK))
        span.end()

    log_structured_event(
        f"Received LLM response for '{agent_name}'",
        event_type="LLM_GENERATION",
        event_phase="OUTCOME",
        agent_name=agent_name,
        session_id=session_id,
        latency_ms=latency_ms,
        outcome_payload={
            "response_parts": part_types,
            "error_code": getattr(llm_response, "error_code", None),
        },
    )
    return None


def before_tool_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
) -> dict[str, Any] | None:
    """Record tool call INTENT (intended action + scrubbed args) before tool runs."""
    session_id = _session_id_from_context(tool_context)
    agent_name = getattr(tool_context, "agent_name", "unknown-agent")
    key = f"tool:{session_id}:{tool.name}"

    scrubbed_args = pii_redactor.scrub_payload(args)
    span = tracer.start_span(f"tool.{tool.name}")
    span.set_attribute("tool.name", tool.name)
    span.set_attribute("agent.name", agent_name)
    span.set_attribute("session.id", session_id)
    _ACTIVE_SPANS[key] = span
    _START_TIMES[key] = time.perf_counter()

    log_structured_event(
        f"Intending to execute tool '{tool.name}'",
        event_type="TOOL_INVOCATION",
        event_phase="INTENT",
        agent_name=agent_name,
        tool_name=tool.name,
        session_id=session_id,
        intent_payload={"tool_name": tool.name, "arguments": scrubbed_args},
    )
    return None


def after_tool_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: dict[str, Any],
) -> dict[str, Any] | None:
    """Record actual tool OUTCOME (status, latency, scrubbed output) after execution."""
    _ = args
    session_id = _session_id_from_context(tool_context)
    agent_name = getattr(tool_context, "agent_name", "unknown-agent")
    key = f"tool:{session_id}:{tool.name}"

    started = _START_TIMES.pop(key, None)
    latency_ms = (time.perf_counter() - started) * 1000.0 if started else 0.0

    result_status = (
        tool_response.get("status", "success")
        if isinstance(tool_response, dict)
        else "success"
    )
    span = _ACTIVE_SPANS.pop(key, None)
    if span is not None:
        span.set_attribute("tool.status", str(result_status))
        span.set_attribute("tool.latency_ms", round(latency_ms, 2))
        span.set_status(
            Status(StatusCode.ERROR if result_status == "error" else StatusCode.OK)
        )
        span.end()

    log_structured_event(
        f"Completed tool '{tool.name}' with status='{result_status}'",
        event_type="TOOL_INVOCATION",
        event_phase="OUTCOME",
        agent_name=agent_name,
        tool_name=tool.name,
        session_id=session_id,
        latency_ms=latency_ms,
        outcome_payload=pii_redactor.scrub_payload(
            tool_response if isinstance(tool_response, dict) else {"result": str(tool_response)}
        ),
        level=logging.WARNING if result_status == "error" else logging.INFO,
    )
    return None


def on_tool_error_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    error: Exception,
) -> dict[str, Any]:
    """Catch any unexpected tool exception and return guided recovery instructions."""
    _ = args
    session_id = _session_id_from_context(tool_context)
    agent_name = getattr(tool_context, "agent_name", "unknown-agent")
    key = f"tool:{session_id}:{tool.name}"

    span = _ACTIVE_SPANS.pop(key, None)
    _START_TIMES.pop(key, None)
    if span is not None:
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()

    fallback = ToolErrorRecoveryPayload(
        error_code="UNEXPECTED_TOOL_EXCEPTION",
        message=f"Tool '{tool.name}' encountered an internal error: {pii_redactor.scrub_text(str(error))}",
        recovery_instructions=(
            "Do not crash or expose raw stack traces. Verify the input parameters or "
            "call create_priority_support_escalation_ticket so a human specialist can assist."
        ),
        retryable=False,
    ).model_dump(mode="json")

    log_structured_event(
        f"Unhandled error in tool '{tool.name}' converted to guided recovery payload",
        event_type="TOOL_INVOCATION",
        event_phase="OUTCOME",
        agent_name=agent_name,
        tool_name=tool.name,
        session_id=session_id,
        outcome_payload=fallback,
        level=logging.ERROR,
    )
    return fallback
