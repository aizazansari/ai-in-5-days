"""Domain-specific E-Commerce Support Tools with strict schemas, guided error recovery, and HITL gates.

All tools follow four AgentOps engineering guarantees:
1. Specific, descriptive verb-noun naming (e.g., ``issue_verified_order_refund``).
2. Comprehensive Google-style docstrings documenting purpose, arguments, and outputs.
3. Strict Pydantic JSON input/output schema validation via ``app.schemas``.
4. Guided error handling returning actionable ``recovery_instructions`` to the LLM
   instead of raising unhandled runtime exceptions.
"""

from __future__ import annotations

import re
from typing import Any

from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from .config import get_settings
from .memory import async_memory_consolidator, ecommerce_store
from .pii import pii_redactor
from .schemas import (
    CustomerMemoryEntrySchema,
    CustomerMemoryQueryInput,
    CustomerMemoryRecallOutput,
    CustomerOrderListInput,
    CustomerOrderListOutput,
    CustomerOrderSummary,
    OrderCancellationInput,
    OrderCancellationOutput,
    OrderDetailsOutput,
    OrderLineItemSchema,
    OrderLookupInput,
    PolicyMatchSchema,
    PriorityEscalationTicketInput,
    PriorityEscalationTicketOutput,
    RefundEligibilityInput,
    RefundEligibilityOutput,
    ShippingAddressUpdateInput,
    ShippingAddressUpdateOutput,
    SupportPolicySearchInput,
    SupportPolicySearchOutput,
    ToolErrorRecoveryPayload,
    VerifiedOrderRefundInput,
    VerifiedOrderRefundOutput,
)

_ORDER_ID_RE = re.compile(r"^ORD-\d{4,}$")
_CUSTOMER_ID_RE = re.compile(r"^CUST-\d{3,}$")


def _guided_validation_error(exc: ValidationError, tool_name: str) -> dict[str, Any]:
    """Convert a Pydantic ValidationError into LLM-actionable recovery instructions."""
    details = "; ".join(
        f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
        for err in exc.errors()
    )
    return ToolErrorRecoveryPayload(
        error_code="SCHEMA_VALIDATION_FAILED",
        message=f"Invalid arguments passed to {tool_name}: {details}",
        recovery_instructions=(
            f"Inspect the field constraints reported above and re-invoke {tool_name} "
            "with corrected parameter types/lengths, or ask the customer for the missing detail."
        ),
        retryable=True,
    ).model_dump(mode="json")


def _normalize_or_guide_order_id(raw_order_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """Validate order ID format ('ORD-XXXX') or return guided recovery instructions."""
    candidate = raw_order_id.strip().upper()
    if not _ORDER_ID_RE.match(candidate):
        suggested = f"ORD-{candidate}" if candidate.isdigit() else "ORD-1001"
        err = ToolErrorRecoveryPayload(
            error_code="INVALID_ORDER_ID_FORMAT",
            message=f"Order ID '{raw_order_id}' does not match required 'ORD-XXXX' format.",
            recovery_instructions=(
                f"If the customer gave digits only, retry with order_id='{suggested}'. "
                "Otherwise call list_recent_customer_orders(customer_id=...) to discover valid order IDs, "
                "or ask the customer for their 'ORD-XXXX' confirmation number."
            ),
            retryable=True,
        ).model_dump(mode="json")
        return None, err
    return candidate, None


def lookup_ecommerce_order_by_id(
    order_id: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Retrieve complete status, line items, delivery dates, and tracking for a single order.

    Use this tool whenever a customer references a specific order number (such as
    ``ORD-1001``) to check shipment status, delivery date, carrier tracking, or
    item details before making policy or refund decisions.

    Args:
        order_id: Unique e-commerce order identifier formatted as ``ORD-XXXX``
            (for example, ``"ORD-1001"``).
        tool_context: Optional ADK ``ToolContext`` used to cache the active order
            and customer ID inside conversational session state.

    Returns:
        JSON dictionary matching ``OrderDetailsOutput`` on success, or
        ``ToolErrorRecoveryPayload`` with recovery instructions if not found.
    """
    try:
        validated = OrderLookupInput.model_validate({"order_id": order_id})
    except ValidationError as exc:
        return _guided_validation_error(exc, "lookup_ecommerce_order_by_id")

    normalized_id, format_err = _normalize_or_guide_order_id(validated.order_id)
    if format_err or normalized_id is None:
        return format_err or {}

    record = ecommerce_store.get_order(normalized_id)
    if record is None:
        return ToolErrorRecoveryPayload(
            error_code="ORDER_NOT_FOUND",
            message=f"No e-commerce order exists with ID '{normalized_id}'.",
            recovery_instructions=(
                "Verify the order number with the customer, or ask for their customer ID "
                "(e.g., 'CUST-101') and call list_recent_customer_orders to locate the right order."
            ),
            retryable=True,
        ).model_dump(mode="json")

    if tool_context is not None:
        tool_context.state["active_order_id"] = record["order_id"]
        tool_context.state["active_customer_id"] = record["customer_id"]

    output = OrderDetailsOutput(
        order_id=record["order_id"],
        customer_id=record["customer_id"],
        order_status=record["order_status"],
        order_date=record["order_date"],
        delivery_date=record["delivery_date"],
        days_since_delivery=record["days_since_delivery"],
        total_amount_usd=float(record["total_amount_usd"]),
        refunded_amount_usd=float(record["refunded_amount_usd"]),
        shipping_address_redacted=pii_redactor.scrub_text(
            record["shipping_destination"]
        ),
        tracking_number=record["tracking_number"],
        carrier=record["carrier"],
        reported_damaged=bool(record["reported_damaged"]),
        items=[OrderLineItemSchema.model_validate(item) for item in record["items"]],
    )
    return output.model_dump(mode="json")


def list_recent_customer_orders(
    customer_id: str,
    limit: int = 5,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """List the most recent e-commerce orders placed by a specific customer.

    Use this tool when a customer does not remember their ``ORD-XXXX`` order ID
    or asks for an overview of all their recent purchases.

    Args:
        customer_id: Unique customer account identifier formatted as ``CUST-XXX``
            (for example, ``"CUST-101"``).
        limit: Maximum number of recent orders to return (1 to 20, default 5).
        tool_context: Optional ADK ``ToolContext`` to persist ``active_customer_id``
            in session state.

    Returns:
        JSON dictionary matching ``CustomerOrderListOutput`` on success, or
        ``ToolErrorRecoveryPayload`` if the customer ID format is invalid or has no orders.
    """
    try:
        validated = CustomerOrderListInput.model_validate(
            {"customer_id": customer_id, "limit": limit}
        )
    except ValidationError as exc:
        return _guided_validation_error(exc, "list_recent_customer_orders")

    normalized_cust = validated.customer_id.upper()
    if not _CUSTOMER_ID_RE.match(normalized_cust):
        return ToolErrorRecoveryPayload(
            error_code="INVALID_CUSTOMER_ID_FORMAT",
            message=f"Customer ID '{customer_id}' must match 'CUST-XXX' format (e.g., 'CUST-101').",
            recovery_instructions=(
                "Ask the customer for their 'CUST-XXX' account ID or a specific 'ORD-XXXX' order ID, "
                "then call lookup_ecommerce_order_by_id instead."
            ),
            retryable=True,
        ).model_dump(mode="json")

    rows = ecommerce_store.list_customer_orders(normalized_cust, limit=validated.limit)
    if not rows:
        return ToolErrorRecoveryPayload(
            error_code="NO_ORDERS_FOR_CUSTOMER",
            message=f"No orders were found under customer account '{normalized_cust}'.",
            recovery_instructions=(
                "Confirm whether the customer placed the order under a different 'CUST-XXX' account "
                "or ask them directly for the 'ORD-XXXX' confirmation number."
            ),
            retryable=True,
        ).model_dump(mode="json")

    if tool_context is not None:
        tool_context.state["active_customer_id"] = normalized_cust

    summaries = [
        CustomerOrderSummary(
            order_id=r["order_id"],
            order_status=r["order_status"],
            order_date=r["order_date"],
            total_amount_usd=float(r["total_amount_usd"]),
            primary_item=(
                r["items"][0]["product_name"] if r["items"] else "Unknown Item"
            ),
        )
        for r in rows
    ]
    return CustomerOrderListOutput(
        customer_id=normalized_cust,
        order_count=len(summaries),
        orders=summaries,
    ).model_dump(mode="json")


def search_ecommerce_return_and_shipping_policies(
    query: str,
    top_k: int = 3,
) -> dict[str, Any]:
    """Perform semantic vector search across official ShopGuard return, refund, and shipping policies.

    Always invoke this tool to ground policy answers, return windows, damaged-item
    exceptions, shipping modification rules, and high-value refund thresholds in
    authoritative store policy rather than guessing.

    Args:
        query: Natural-language policy question or topic (for example,
            ``"damaged smartwatch return window and shipping fee waiver"``).
        top_k: Number of highest-similarity policy documents to retrieve (1 to 5).

    Returns:
        JSON dictionary matching ``SupportPolicySearchOutput`` with ranked policy snippets.
    """
    try:
        validated = SupportPolicySearchInput.model_validate(
            {"query": query, "top_k": top_k}
        )
    except ValidationError as exc:
        return _guided_validation_error(
            exc, "search_ecommerce_return_and_shipping_policies"
        )

    matches = ecommerce_store.search_policies(
        query=validated.query, top_k=validated.top_k
    )
    return SupportPolicySearchOutput(
        query=validated.query,
        matches=[PolicyMatchSchema.model_validate(m) for m in matches],
    ).model_dump(mode="json")


def recall_customer_support_history(
    customer_id: str,
    topic_query: str = "recent support issues and preferences",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Retrieve persistent, PII-redacted long-term memories and preferences for a customer.

    Queries the persistent customer vector memory store to personalize support
    across multi-session interactions (such as preferred return carrier or prior
    damaged-package reports).

    Args:
        customer_id: Customer identifier formatted as ``CUST-XXX`` (e.g., ``"CUST-101"``).
        topic_query: Semantic search phrase used to rank relevant historical notes.
        tool_context: Optional ADK ``ToolContext`` to cache recalled context in session state.

    Returns:
        JSON dictionary matching ``CustomerMemoryRecallOutput`` on success, or
        ``ToolErrorRecoveryPayload`` if the customer ID is malformed.
    """
    try:
        validated = CustomerMemoryQueryInput.model_validate(
            {"customer_id": customer_id, "topic_query": topic_query}
        )
    except ValidationError as exc:
        return _guided_validation_error(exc, "recall_customer_support_history")

    normalized_cust = validated.customer_id.upper()
    if not _CUSTOMER_ID_RE.match(normalized_cust):
        return ToolErrorRecoveryPayload(
            error_code="INVALID_CUSTOMER_ID_FORMAT",
            message=f"Customer ID '{customer_id}' does not match 'CUST-XXX' format.",
            recovery_instructions=(
                "If you only have an order ID (e.g. 'ORD-1001'), call lookup_ecommerce_order_by_id first "
                "to obtain the associated 'customer_id', then retry recall_customer_support_history."
            ),
            retryable=True,
        ).model_dump(mode="json")

    memories = ecommerce_store.search_customer_memories(
        customer_id=normalized_cust,
        topic_query=validated.topic_query,
    )
    if tool_context is not None:
        tool_context.state["active_customer_id"] = normalized_cust
        tool_context.state["recalled_memories_count"] = len(memories)

    return CustomerMemoryRecallOutput(
        customer_id=normalized_cust,
        memories=[CustomerMemoryEntrySchema.model_validate(m) for m in memories],
    ).model_dump(mode="json")


def update_unshipped_order_shipping_address(
    order_id: str,
    new_city_state_zip: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Update the shipping destination on an order that is still in 'processing' status.

    Enforces store shipping policy (`POL-SHIPPING-MOD`): orders that have already
    shipped, been delivered, or been cancelled cannot have their destination changed.

    Args:
        order_id: Target order identifier in ``ORD-XXXX`` format (e.g., ``"ORD-1003"``).
        new_city_state_zip: Updated destination city, state, and postal code
            (for example, ``"Austin, TX 78701"``). Street numbers are automatically
            redacted before storage.
        tool_context: Optional ADK ``ToolContext`` for session tracking.

    Returns:
        JSON dictionary matching ``ShippingAddressUpdateOutput`` on success, or
        ``ToolErrorRecoveryPayload`` explaining alternatives if the order already shipped.
    """
    try:
        validated = ShippingAddressUpdateInput.model_validate(
            {"order_id": order_id, "new_city_state_zip": new_city_state_zip}
        )
    except ValidationError as exc:
        return _guided_validation_error(exc, "update_unshipped_order_shipping_address")

    normalized_id, format_err = _normalize_or_guide_order_id(validated.order_id)
    if format_err or normalized_id is None:
        return format_err or {}

    order = ecommerce_store.get_order(normalized_id)
    if order is None:
        return ToolErrorRecoveryPayload(
            error_code="ORDER_NOT_FOUND",
            message=f"Order '{normalized_id}' was not found.",
            recovery_instructions=(
                "Call list_recent_customer_orders to confirm the exact order ID before retrying."
            ),
            retryable=True,
        ).model_dump(mode="json")

    if order["order_status"] != "processing":
        return ToolErrorRecoveryPayload(
            error_code="ORDER_ALREADY_DISPATCHED",
            message=(
                f"Cannot modify shipping address for '{normalized_id}' because its "
                f"current status is '{order['order_status']}'."
            ),
            recovery_instructions=(
                "Inform the customer that shipped/delivered packages cannot be rerouted in transit "
                "per policy POL-SHIPPING-MOD. Offer to initiate a return once delivered, or call "
                "create_priority_support_escalation_ticket if carrier interception is urgently needed."
            ),
            retryable=False,
        ).model_dump(mode="json")

    prev_dest = order["shipping_destination"]
    updated = ecommerce_store.update_shipping_destination(
        normalized_id, validated.new_city_state_zip
    )

    # Non-blocking async memory write recording the address modification
    async_memory_consolidator.schedule_consolidation(
        customer_id=order["customer_id"],
        interaction_summary=(
            f"Updated shipping destination for unshipped order {normalized_id} "
            f"to {updated['shipping_destination']}."
        ),
    )

    if tool_context is not None:
        tool_context.state["last_updated_order_id"] = normalized_id

    return ShippingAddressUpdateOutput(
        order_id=normalized_id,
        previous_destination=prev_dest,
        updated_destination=updated["shipping_destination"],
        confirmation_note=(
            f"Shipping destination for {normalized_id} updated prior to warehouse dispatch."
        ),
    ).model_dump(mode="json")


def cancel_unshipped_customer_order(
    order_id: str,
    cancellation_reason: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Cancel an unshipped ('processing') customer order and void its payment authorization.

    Wrapped with ``FunctionTool(..., require_confirmation=True)`` in agent registration
    because order cancellation is irreversible and requires explicit Human-in-the-Loop
    confirmation.

    Args:
        order_id: Order identifier in ``ORD-XXXX`` format (e.g., ``"ORD-1003"``).
        cancellation_reason: Customer's stated reason for cancelling the order.
        tool_context: Optional ADK ``ToolContext`` providing confirmation state.

    Returns:
        JSON dictionary matching ``OrderCancellationOutput`` on success, or
        ``ToolErrorRecoveryPayload`` if the order cannot be cancelled.
    """
    try:
        validated = OrderCancellationInput.model_validate(
            {"order_id": order_id, "cancellation_reason": cancellation_reason}
        )
    except ValidationError as exc:
        return _guided_validation_error(exc, "cancel_unshipped_customer_order")

    normalized_id, format_err = _normalize_or_guide_order_id(validated.order_id)
    if format_err or normalized_id is None:
        return format_err or {}

    order = ecommerce_store.get_order(normalized_id)
    if order is None:
        return ToolErrorRecoveryPayload(
            error_code="ORDER_NOT_FOUND",
            message=f"Order '{normalized_id}' does not exist.",
            recovery_instructions=(
                "Check the customer's recent orders with list_recent_customer_orders before retrying."
            ),
            retryable=True,
        ).model_dump(mode="json")

    if order["order_status"] != "processing":
        return ToolErrorRecoveryPayload(
            error_code="CANCELLATION_WINDOW_CLOSED",
            message=(
                f"Order '{normalized_id}' is currently '{order['order_status']}' and can no longer "
                "be cancelled prior to shipment."
            ),
            recovery_instructions=(
                "If the order is 'delivered', call evaluate_order_refund_eligibility to check return/refund "
                "options instead. If 'shipped', advise waiting for delivery to initiate a 30-day return."
            ),
            retryable=False,
        ).model_dump(mode="json")

    ecommerce_store.cancel_order(normalized_id)
    async_memory_consolidator.schedule_consolidation(
        customer_id=order["customer_id"],
        interaction_summary=(
            f"Cancelled processing order {normalized_id} (${order['total_amount_usd']:.2f}) "
            f"due to: {validated.cancellation_reason}."
        ),
    )

    if tool_context is not None:
        tool_context.state["last_cancelled_order_id"] = normalized_id

    return OrderCancellationOutput(
        order_id=normalized_id,
        released_authorization_usd=float(order["total_amount_usd"]),
        confirmation_message=(
            f"Order {normalized_id} has been cancelled and the ${order['total_amount_usd']:.2f} "
            "payment hold has been released."
        ),
    ).model_dump(mode="json")


def evaluate_order_refund_eligibility(
    order_id: str,
    reason_category: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Evaluate whether an e-commerce order qualifies for a refund under ShopGuard policy.

    Applies deterministic policy rules (`POL-RETURN-30D`, `POL-DAMAGED-60D`,
    `POL-HIGH-VALUE-HITL`, and `POL-LATE-DELIVERY`) against the order's delivery
    date, final-sale flags, remaining refundable balance, and supervisor approval
    threshold ($100.00 USD).

    Args:
        order_id: Order identifier in ``ORD-XXXX`` format (e.g., ``"ORD-1001"``).
        reason_category: Must be one of ``"damaged_or_defective"``,
            ``"wrong_item_received"``, ``"changed_mind"``, or ``"late_delivery"``.
        tool_context: Optional ADK ``ToolContext`` used to record verified eligibility
            in session state prior to calling ``issue_verified_order_refund``.

    Returns:
        JSON dictionary matching ``RefundEligibilityOutput`` on success, or
        ``ToolErrorRecoveryPayload`` if inputs are invalid or order is missing.
    """
    try:
        validated = RefundEligibilityInput.model_validate(
            {"order_id": order_id, "reason_category": reason_category}
        )
    except ValidationError as exc:
        return _guided_validation_error(exc, "evaluate_order_refund_eligibility")

    normalized_id, format_err = _normalize_or_guide_order_id(validated.order_id)
    if format_err or normalized_id is None:
        return format_err or {}

    order = ecommerce_store.get_order(normalized_id)
    if order is None:
        return ToolErrorRecoveryPayload(
            error_code="ORDER_NOT_FOUND",
            message=f"Order '{normalized_id}' was not found.",
            recovery_instructions=(
                "Use lookup_ecommerce_order_by_id or list_recent_customer_orders to confirm a valid order ID."
            ),
            retryable=True,
        ).model_dump(mode="json")

    settings = get_settings()
    remaining_balance = round(
        max(0.0, float(order["total_amount_usd"]) - float(order["refunded_amount_usd"])),
        2,
    )
    status = order["order_status"]
    days = order["days_since_delivery"]
    has_final_sale = any(bool(item.get("is_final_sale")) for item in order["items"])
    is_merchant_fault = validated.reason_category in (
        "damaged_or_defective",
        "wrong_item_received",
    )

    if status == "refunded" or remaining_balance <= 0.0:
        result = RefundEligibilityOutput(
            order_id=normalized_id,
            eligible=False,
            max_refundable_usd=0.0,
            requires_human_supervisor_approval=False,
            policy_rule_applied="ALREADY_REFUNDED",
            recommended_next_action="Inform the customer that this order has already been fully refunded.",
        )
    elif status == "cancelled":
        result = RefundEligibilityOutput(
            order_id=normalized_id,
            eligible=False,
            max_refundable_usd=0.0,
            requires_human_supervisor_approval=False,
            policy_rule_applied="ORDER_CANCELLED",
            recommended_next_action="Explain that cancelled orders have already voided their payment hold.",
        )
    elif status == "processing":
        result = RefundEligibilityOutput(
            order_id=normalized_id,
            eligible=False,
            max_refundable_usd=0.0,
            requires_human_supervisor_approval=False,
            policy_rule_applied="POL-SHIPPING-MOD",
            recommended_next_action=(
                "Order has not shipped yet; call cancel_unshipped_customer_order "
                "instead of issuing a post-delivery refund."
            ),
        )
    elif status == "shipped":
        result = RefundEligibilityOutput(
            order_id=normalized_id,
            eligible=False,
            max_refundable_usd=0.0,
            requires_human_supervisor_approval=False,
            policy_rule_applied="POL-LATE-DELIVERY",
            recommended_next_action=(
                "Order is actively in transit; call create_priority_support_escalation_ticket for carrier trace "
                "or wait until delivery to process a return."
            ),
        )
    elif is_merchant_fault and (days is None or days <= 60):
        needs_hitl = remaining_balance > settings.high_value_refund_threshold_usd
        result = RefundEligibilityOutput(
            order_id=normalized_id,
            eligible=True,
            max_refundable_usd=remaining_balance,
            requires_human_supervisor_approval=needs_hitl,
            policy_rule_applied="POL-DAMAGED-60D",
            recommended_next_action=(
                f"Proceed to issue_verified_order_refund for up to ${remaining_balance:.2f} "
                + (
                    "(Human supervisor confirmation will be triggered automatically)."
                    if needs_hitl
                    else "(Eligible for instant automated refund)."
                )
            ),
        )
    elif has_final_sale and not is_merchant_fault:
        result = RefundEligibilityOutput(
            order_id=normalized_id,
            eligible=False,
            max_refundable_usd=0.0,
            requires_human_supervisor_approval=False,
            policy_rule_applied="POL-RETURN-30D (Final Sale Exclusion)",
            recommended_next_action=(
                "Explain that final-sale clearance items cannot be refunded for changed_mind, "
                "or call create_priority_support_escalation_ticket for executive store-credit review."
            ),
        )
    elif days is not None and days <= 30:
        needs_hitl = remaining_balance > settings.high_value_refund_threshold_usd
        result = RefundEligibilityOutput(
            order_id=normalized_id,
            eligible=True,
            max_refundable_usd=remaining_balance,
            requires_human_supervisor_approval=needs_hitl,
            policy_rule_applied="POL-RETURN-30D",
            recommended_next_action=(
                f"Proceed to issue_verified_order_refund for up to ${remaining_balance:.2f}."
            ),
        )
    else:
        result = RefundEligibilityOutput(
            order_id=normalized_id,
            eligible=False,
            max_refundable_usd=0.0,
            requires_human_supervisor_approval=False,
            policy_rule_applied="POL-RETURN-30D (Window Expired)",
            recommended_next_action=(
                f"Delivery was {days} days ago (exceeds 30-day window). Politely decline the direct refund "
                "and offer create_priority_support_escalation_ticket if an exception review is needed."
            ),
        )

    payload = result.model_dump(mode="json")
    if tool_context is not None:
        verified_map = dict(tool_context.state.get("verified_refund_eligibility", {}))
        verified_map[normalized_id] = payload
        tool_context.state["verified_refund_eligibility"] = verified_map

    return payload


async def requires_supervisor_refund_approval(
    order_id: str,
    refund_amount_usd: float,
    reason: str,
    tool_context: ToolContext | None = None,
) -> bool:
    """ADK Human-in-the-Loop predicate pausing refunds above the $100.00 threshold."""
    _ = (order_id, reason, tool_context)
    settings = get_settings()
    return float(refund_amount_usd) > settings.high_value_refund_threshold_usd


def issue_verified_order_refund(
    order_id: str,
    refund_amount_usd: float,
    reason: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Disburse a verified USD refund back to the customer's original payment method.

    Protected by two safety layers:
    1. Eligibility verification: Checks that the order is delivered and the refund
       amount does not exceed the remaining refundable order balance.
    2. Human-in-the-Loop (HITL) confirmation: Wrapped via ``FunctionTool`` with
       ``require_confirmation=requires_supervisor_refund_approval`` so any refund
       above ``HIGH_VALUE_REFUND_THRESHOLD_USD`` ($100.00) pauses execution until
       explicitly approved by a human supervisor.

    Args:
        order_id: Order identifier in ``ORD-XXXX`` format (e.g., ``"ORD-1001"``).
        refund_amount_usd: Positive dollar amount to refund (must be <= remaining balance).
        reason: Audit justification recorded in the financial refund ledger.
        tool_context: Optional ADK ``ToolContext`` carrying session and HITL confirmation state.

    Returns:
        JSON dictionary matching ``VerifiedOrderRefundOutput`` on success, or
        ``ToolErrorRecoveryPayload`` with recovery instructions if ineligible or rejected.
    """
    try:
        validated = VerifiedOrderRefundInput.model_validate(
            {
                "order_id": order_id,
                "refund_amount_usd": refund_amount_usd,
                "reason": reason,
            }
        )
    except ValidationError as exc:
        return _guided_validation_error(exc, "issue_verified_order_refund")

    normalized_id, format_err = _normalize_or_guide_order_id(validated.order_id)
    if format_err or normalized_id is None:
        return format_err or {}

    # Check if ADK ToolConfirmation explicitly rejected the high-stakes action
    if (
        tool_context is not None
        and getattr(tool_context, "tool_confirmation", None) is not None
        and not tool_context.tool_confirmation.confirmed
    ):
        return ToolErrorRecoveryPayload(
            error_code="SUPERVISOR_CONFIRMATION_REJECTED",
            message=(
                f"Human supervisor declined the ${validated.refund_amount_usd:.2f} refund "
                f"for order '{normalized_id}'."
            ),
            recovery_instructions=(
                "Do NOT retry issue_verified_order_refund. Inform the customer that the high-value "
                "refund was not approved for automated disbursement and call "
                "create_priority_support_escalation_ticket for manual billing review."
            ),
            retryable=False,
        ).model_dump(mode="json")

    order = ecommerce_store.get_order(normalized_id)
    if order is None:
        return ToolErrorRecoveryPayload(
            error_code="ORDER_NOT_FOUND",
            message=f"Order '{normalized_id}' does not exist.",
            recovery_instructions=(
                "Call lookup_ecommerce_order_by_id or list_recent_customer_orders to verify the order ID."
            ),
            retryable=True,
        ).model_dump(mode="json")

    if order["order_status"] != "delivered":
        return ToolErrorRecoveryPayload(
            error_code="ORDER_NOT_DELIVERED",
            message=(
                f"Cannot issue a refund on '{normalized_id}' because status is '{order['order_status']}'."
            ),
            recovery_instructions=(
                "Call evaluate_order_refund_eligibility first. For 'processing' orders use "
                "cancel_unshipped_customer_order; for 'shipped' orders use create_priority_support_escalation_ticket."
            ),
            retryable=False,
        ).model_dump(mode="json")

    remaining_balance = round(
        max(0.0, float(order["total_amount_usd"]) - float(order["refunded_amount_usd"])),
        2,
    )
    if validated.refund_amount_usd > remaining_balance:
        return ToolErrorRecoveryPayload(
            error_code="REFUND_EXCEEDS_ORDER_BALANCE",
            message=(
                f"Requested refund (${validated.refund_amount_usd:.2f}) exceeds remaining "
                f"refundable balance (${remaining_balance:.2f}) for '{normalized_id}'."
            ),
            recovery_instructions=(
                f"Retry issue_verified_order_refund with refund_amount_usd={remaining_balance} "
                "if a partial/remaining balance remains, or inform the customer if already refunded."
            ),
            retryable=remaining_balance > 0.0,
        ).model_dump(mode="json")

    # Enforce 30-day / final-sale policy unless damaged/defective
    days = order["days_since_delivery"]
    has_final_sale = any(bool(item.get("is_final_sale")) for item in order["items"])
    reason_lower = validated.reason.lower()
    is_damaged_claim = order["reported_damaged"] or any(
        kw in reason_lower for kw in ("damage", "defect", "broken", "wrong item", "cracked")
    )
    if not is_damaged_claim and (has_final_sale or (days is not None and days > 30)):
        return ToolErrorRecoveryPayload(
            error_code="POLICY_INELIGIBLE_FOR_REFUND",
            message=(
                f"Order '{normalized_id}' fails standard return policy (days_since_delivery={days}, "
                f"final_sale={has_final_sale})."
            ),
            recovery_instructions=(
                "Call evaluate_order_refund_eligibility to inspect the exact policy rule, then "
                "offer create_priority_support_escalation_ticket if the customer requests an exception."
            ),
            retryable=False,
        ).model_dump(mode="json")

    settings = get_settings()
    supervisor_approved = (
        validated.refund_amount_usd > settings.high_value_refund_threshold_usd
    )
    ledger_entry = ecommerce_store.record_refund(
        order_id=normalized_id,
        refund_amount_usd=validated.refund_amount_usd,
        reason=validated.reason,
        supervisor_approved=supervisor_approved,
    )

    # Non-blocking background consolidation into persistent customer memory
    async_memory_consolidator.schedule_consolidation(
        customer_id=order["customer_id"],
        interaction_summary=(
            f"Issued ${validated.refund_amount_usd:.2f} refund ({ledger_entry['refund_id']}) "
            f"on order {normalized_id}. Reason: {validated.reason}."
        ),
    )

    if tool_context is not None:
        tool_context.state["last_refund_id"] = ledger_entry["refund_id"]

    return VerifiedOrderRefundOutput(
        refund_id=ledger_entry["refund_id"],
        order_id=normalized_id,
        refunded_amount_usd=ledger_entry["refunded_amount_usd"],
        order_status=ledger_entry["order_status"],
        supervisor_approved=supervisor_approved,
    ).model_dump(mode="json")


def create_priority_support_escalation_ticket(
    order_id: str,
    customer_id: str,
    severity: str,
    issue_summary: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Create a structured Tier-2 human escalation ticket for unresolved or out-of-policy disputes.

    Use this tool when an order is delayed in transit, past the 30-day return window
    requiring executive review, or when a high-value supervisor confirmation was declined.

    Args:
        order_id: Associated order identifier in ``ORD-XXXX`` format (e.g., ``"ORD-1002"``).
        customer_id: Customer account identifier in ``CUST-XXX`` format (e.g., ``"CUST-101"``).
        severity: SLA urgency tier: ``"low"``, ``"medium"``, ``"high"``, or ``"critical"``.
        issue_summary: Concise summary of the customer's issue (any PII is automatically redacted).
        tool_context: Optional ADK ``ToolContext`` to store the generated ticket ID in state.

    Returns:
        JSON dictionary matching ``PriorityEscalationTicketOutput`` on success, or
        ``ToolErrorRecoveryPayload`` if validation fails.
    """
    try:
        validated = PriorityEscalationTicketInput.model_validate(
            {
                "order_id": order_id,
                "customer_id": customer_id,
                "severity": severity,
                "issue_summary": issue_summary,
            }
        )
    except ValidationError as exc:
        return _guided_validation_error(
            exc, "create_priority_support_escalation_ticket"
        )

    normalized_id, format_err = _normalize_or_guide_order_id(validated.order_id)
    if format_err or normalized_id is None:
        return format_err or {}

    normalized_cust = validated.customer_id.upper()
    if not _CUSTOMER_ID_RE.match(normalized_cust):
        return ToolErrorRecoveryPayload(
            error_code="INVALID_CUSTOMER_ID_FORMAT",
            message=f"Customer ID '{customer_id}' must match 'CUST-XXX' format.",
            recovery_instructions=(
                f"Call lookup_ecommerce_order_by_id(order_id='{normalized_id}') to fetch the "
                "order's exact customer_id, then retry create_priority_support_escalation_ticket."
            ),
            retryable=True,
        ).model_dump(mode="json")

    ticket = ecommerce_store.create_escalation_ticket(
        order_id=normalized_id,
        customer_id=normalized_cust,
        severity=validated.severity,
        issue_summary=validated.issue_summary,
    )

    async_memory_consolidator.schedule_consolidation(
        customer_id=normalized_cust,
        interaction_summary=(
            f"Opened {validated.severity} escalation ticket {ticket['ticket_id']} "
            f"for order {normalized_id}: {validated.issue_summary}"
        ),
    )

    if tool_context is not None:
        tool_context.state["last_escalation_ticket_id"] = ticket["ticket_id"]

    return PriorityEscalationTicketOutput.model_validate(ticket).model_dump(mode="json")


# Wrap high-stakes financial & irreversible mutation tools with ADK HITL FunctionTool gates
hitl_issue_verified_order_refund_tool = FunctionTool(
    issue_verified_order_refund,
    require_confirmation=requires_supervisor_refund_approval,
)

hitl_cancel_unshipped_customer_order_tool = FunctionTool(
    cancel_unshipped_customer_order,
    require_confirmation=True,
)
