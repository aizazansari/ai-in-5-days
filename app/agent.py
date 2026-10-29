"""Multi-Agent E-Commerce Order Support Architecture with Strategic Model Routing.

Architecture Overview:
- ``root_agent`` (``ecommerce_support_coordinator``, ``gemini-2.5-flash``):
  Low-latency front-door coordinator handling customer greeting, memory recall,
  fast order tracking, and intent-based delegation to specialized sub-agents:
    1. ``policy_and_shipping_specialist`` (``LlmAgent``, ``gemini-2.5-flash``):
       Fast semantic policy retrieval, unshipped shipping updates, and HITL-gated
       order cancellations.
    2. ``refund_resolution_pipeline`` (``SequentialAgent``):
       Two-stage deep-reasoning pipeline powered by ``gemini-2.5-pro``:
         a) ``refund_eligibility_investigator`` -> gathers order facts, policy docs,
            and deterministic eligibility into ``state['eligibility_assessment']``.
         b) ``refund_execution_and_escalation_specialist`` -> executes HITL-gated
            verified refunds or creates Tier-2 priority escalation tickets.

Wrapped in an ADK ``App`` configured with:
- ``EventsCompactionConfig`` (token-based + tail-retention history compaction)
- ``ContextCacheConfig`` (Gemini context caching for constitutional instructions)
- ``ResumabilityConfig(is_resumable=True)`` (pause/resume support for HITL confirmations)
- ``EcommerceSecurityAndPolicyPlugin`` (global prompt-injection, financial cap, & PII guardrails)
"""

from __future__ import annotations

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.app import App, EventsCompactionConfig, ResumabilityConfig

from .callbacks import (
    after_agent_callback,
    after_model_callback,
    after_tool_callback,
    before_agent_callback,
    before_model_callback,
    before_tool_callback,
    on_tool_error_callback,
)
from .config import get_settings
from .guardrails import EcommerceSecurityAndPolicyPlugin
from .prompts import (
    COORDINATOR_CONSTITUTION_PROMPT,
    POLICY_AND_SHIPPING_SPECIALIST_PROMPT,
    REFUND_EXECUTION_SPECIALIST_PROMPT,
    REFUND_INVESTIGATOR_PROMPT,
)
from .tools import (
    create_priority_support_escalation_ticket,
    evaluate_order_refund_eligibility,
    hitl_cancel_unshipped_customer_order_tool,
    hitl_issue_verified_order_refund_tool,
    list_recent_customer_orders,
    lookup_ecommerce_order_by_id,
    recall_customer_support_history,
    search_ecommerce_return_and_shipping_policies,
    update_unshipped_order_shipping_address,
)

settings = get_settings()

# Shared observability & error-recovery callback kwargs applied to every LlmAgent
_LLM_OBSERVABILITY_HOOKS = {
    "before_agent_callback": before_agent_callback,
    "after_agent_callback": after_agent_callback,
    "before_model_callback": before_model_callback,
    "after_model_callback": after_model_callback,
    "before_tool_callback": before_tool_callback,
    "after_tool_callback": after_tool_callback,
    "on_tool_error_callback": on_tool_error_callback,
}

# --- Sub-Agent 1: Fast Policy & Pre-Shipment Specialist (gemini-2.5-flash) ---
policy_and_shipping_specialist = LlmAgent(
    name="policy_and_shipping_specialist",
    model=settings.fast_router_model,
    description=(
        "Handles ShopGuard return/shipping policy questions, shipping address updates "
        "for unshipped orders, and HITL-confirmed cancellations for unshipped orders."
    ),
    instruction=POLICY_AND_SHIPPING_SPECIALIST_PROMPT,
    tools=[
        lookup_ecommerce_order_by_id,
        search_ecommerce_return_and_shipping_policies,
        update_unshipped_order_shipping_address,
        hitl_cancel_unshipped_customer_order_tool,
    ],
    **_LLM_OBSERVABILITY_HOOKS,
)

# --- Sub-Agent 2: Deep-Reasoning Refund Resolution Pipeline (gemini-2.5-pro) ---
refund_eligibility_investigator = LlmAgent(
    name="refund_eligibility_investigator",
    model=settings.reasoning_planner_model,
    description=(
        "Stage 1 investigator that cross-examines order records, policy rules, and "
        "refund eligibility criteria before any money is disbursed."
    ),
    instruction=REFUND_INVESTIGATOR_PROMPT,
    output_key="eligibility_assessment",
    tools=[
        lookup_ecommerce_order_by_id,
        search_ecommerce_return_and_shipping_policies,
        evaluate_order_refund_eligibility,
    ],
    **_LLM_OBSERVABILITY_HOOKS,
)

refund_execution_and_escalation_specialist = LlmAgent(
    name="refund_execution_and_escalation_specialist",
    model=settings.reasoning_planner_model,
    description=(
        "Stage 2 specialist that executes verified refunds (with supervisor HITL gate "
        "above $100) or files priority Tier-2 escalation tickets."
    ),
    instruction=REFUND_EXECUTION_SPECIALIST_PROMPT,
    tools=[
        hitl_issue_verified_order_refund_tool,
        create_priority_support_escalation_ticket,
    ],
    **_LLM_OBSERVABILITY_HOOKS,
)

refund_resolution_pipeline = SequentialAgent(
    name="refund_resolution_pipeline",
    description=(
        "Multi-step sequential workflow for all refund requests, damaged/wrong item "
        "claims, billing disputes, and delayed-shipment escalations."
    ),
    sub_agents=[
        refund_eligibility_investigator,
        refund_execution_and_escalation_specialist,
    ],
    before_agent_callback=before_agent_callback,
    after_agent_callback=after_agent_callback,
)

# --- Root Coordinator Agent (gemini-2.5-flash) ---
root_agent = LlmAgent(
    name="ecommerce_support_coordinator",
    model=settings.fast_router_model,
    description=(
        "Front-door E-Commerce Order Support Coordinator that triages customer queries, "
        "recalls persistent customer history, checks order status, and routes complex "
        "policy/shipping or refund workflows to specialist sub-agents."
    ),
    instruction=COORDINATOR_CONSTITUTION_PROMPT,
    tools=[
        lookup_ecommerce_order_by_id,
        list_recent_customer_orders,
        recall_customer_support_history,
    ],
    sub_agents=[
        policy_and_shipping_specialist,
        refund_resolution_pipeline,
    ],
    **_LLM_OBSERVABILITY_HOOKS,
)

# --- ADK App with Context Compaction, Context Caching, Resumability, & Global Guardrail Plugin ---
compaction_config = EventsCompactionConfig(
    token_threshold=4000,
    event_retention_size=6,
)

context_cache_config = ContextCacheConfig(
    min_tokens=2048,
    ttl_seconds=600,
    cache_intervals=5,
)

app = App(
    name=settings.app_name,
    root_agent=root_agent,
    events_compaction_config=compaction_config,
    context_cache_config=context_cache_config,
    resumability_config=ResumabilityConfig(is_resumable=True),
    plugins=[EcommerceSecurityAndPolicyPlugin()],
)
