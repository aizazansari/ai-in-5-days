"""Global ADK Security, Policy Enforcement, and Self-Evaluation Guardrail Plugin.

Implements ``EcommerceSecurityAndPolicyPlugin(BasePlugin)`` registered on the ADK
``App`` so guardrails apply globally across the Coordinator and all Sub-Agents:
1. Input Prompt-Injection & Jailbreak Screening + Input PII Scrubbing.
2. Pre-Tool Financial Policy Guardrails (caps, negative/unbounded refund prevention).
3. Output Self-Evaluation & PII Redaction (prevents PII leaks and unauthorized
   compensation promises such as >100% refunds or free-gift hallucinations).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .observability import log_structured_event
from .pii import pii_redactor
from .schemas import ToolErrorRecoveryPayload

_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|system)\s+instructions"),
    re.compile(r"(?i)reveal\s+(your\s+)?(system\s+prompt|constitution|api\s+key|secret)"),
    re.compile(r"(?i)bypass\s+(supervisor|confirmation|approval|policy|guardrail)"),
    re.compile(r"(?i)you\s+are\s+now\s+(in\s+)?developer\s+mode"),
    re.compile(r"(?i)issue\s+a\s+\$?\d{4,}\s+refund\s+without\s+checking"),
)

_UNAUTHORIZED_PROMISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:200%|double|triple)\s+refund\b"),
    re.compile(r"(?i)\bunlimited\s+free\s+store\s+credit\b"),
)

MAX_SINGLE_REFUND_CAP_USD = 2500.0


def inspect_prompt_security(user_text: str) -> str | None:
    """Return a violation code if the user message contains prompt-injection patterns."""
    for pattern in _PROMPT_INJECTION_PATTERNS:
        if pattern.search(user_text):
            return f"PROMPT_INJECTION_DETECTED:{pattern.pattern}"
    return None


def evaluate_response_compliance(response_text: str) -> tuple[bool, str]:
    """Self-evaluation check verifying model output adheres to policy & PII rules."""
    scrubbed = pii_redactor.scrub_text(response_text)
    for pattern in _UNAUTHORIZED_PROMISE_PATTERNS:
        if pattern.search(scrubbed):
            safe_replacement = (
                "I can only offer refunds up to the verified purchase amount on eligible "
                "orders per ShopGuard policy, or open a Tier-2 escalation ticket for review."
            )
            return False, safe_replacement
    return (scrubbed == response_text), scrubbed


class EcommerceSecurityAndPolicyPlugin(BasePlugin):
    """ADK global plugin enforcing input security, tool policies, and output self-eval."""

    def __init__(self) -> None:
        super().__init__(name="ecommerce_security_and_policy_guardrail")

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        """Screen user inputs for prompt injection and scrub raw PII before LLM invocation."""
        contents = getattr(llm_request, "contents", None) or []
        if not contents:
            return None

        # Inspect and sanitize the latest user turn
        latest_content = contents[-1]
        if getattr(latest_content, "role", None) == "user":
            for part in getattr(latest_content, "parts", None) or []:
                text = getattr(part, "text", None)
                if not text:
                    continue

                violation = inspect_prompt_security(text)
                if violation is not None:
                    log_structured_event(
                        "Blocked unsafe prompt via security guardrail plugin",
                        event_type="GUARDRAIL_INTERVENTION",
                        event_phase="OUTCOME",
                        agent_name=callback_context.agent_name,
                        outcome_payload={"violation": violation},
                        level=logging.WARNING,
                    )
                    return LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part.from_text(
                                    text=(
                                        "I cannot override ShopGuard security policies, supervisor "
                                        "confirmation rules, or system instructions. Please share "
                                        "your order ID (e.g., ORD-1001) and how I can help with your order."
                                    )
                                )
                            ],
                        )
                    )

                # Redact sensitive PII (such as raw payment card numbers) in-place before sending to LLM
                part.text = pii_redactor.scrub_text(text)

        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Enforce hard financial guardrails prior to executing refund mutations."""
        if tool.name == "issue_verified_order_refund":
            amount = float(tool_args.get("refund_amount_usd", 0.0) or 0.0)
            if amount <= 0.0 or amount > MAX_SINGLE_REFUND_CAP_USD:
                blocked = ToolErrorRecoveryPayload(
                    error_code="POLICY_GUARDRAIL_REFUND_CAP",
                    message=(
                        f"Guardrail blocked refund of ${amount:.2f}: single automated refund "
                        f"must be between $0.01 and ${MAX_SINGLE_REFUND_CAP_USD:.2f}."
                    ),
                    recovery_instructions=(
                        "Verify the order's actual total_amount_usd via lookup_ecommerce_order_by_id, "
                        "or escalate amounts above $2,500 via create_priority_support_escalation_ticket."
                    ),
                    retryable=False,
                ).model_dump(mode="json")

                log_structured_event(
                    "Guardrail plugin blocked out-of-bounds tool call",
                    event_type="GUARDRAIL_INTERVENTION",
                    event_phase="OUTCOME",
                    agent_name=getattr(tool_context, "agent_name", "unknown"),
                    tool_name=tool.name,
                    outcome_payload=blocked,
                    level=logging.WARNING,
                )
                return blocked
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        """Run self-evaluation policy check and PII scrubbing on outgoing LLM responses."""
        content = getattr(llm_response, "content", None)
        if content is None:
            return None

        modified = False
        new_parts: list[types.Part] = []
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                compliant, sanitized_text = evaluate_response_compliance(text)
                if not compliant or sanitized_text != text:
                    modified = True
                    new_parts.append(types.Part.from_text(text=sanitized_text))
                    log_structured_event(
                        "Output guardrail sanitized model response",
                        event_type="GUARDRAIL_SELF_EVAL",
                        event_phase="OUTCOME",
                        agent_name=callback_context.agent_name,
                        outcome_payload={"compliant": compliant},
                        level=logging.WARNING,
                    )
                else:
                    new_parts.append(part)
            else:
                new_parts.append(part)

        if modified:
            return LlmResponse(
                content=types.Content(
                    role=getattr(content, "role", "model") or "model",
                    parts=new_parts,
                )
            )
        return None
