"""Strict Pydantic JSON Schemas for tool inputs, outputs, and error recovery.

Enforces explicit schema validation on every tool boundary to constrain LLM
arguments, prevent hallucinated fields (``extra="forbid"``), and standardize
guided recovery error responses.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictSchemaModel(BaseModel):
    """Base model enforcing strict JSON schema validation with no extra fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolErrorRecoveryPayload(StrictSchemaModel):
    """Guided error response instructing the LLM how to recover gracefully."""

    status: Literal["error"] = "error"
    error_code: str = Field(
        ...,
        description="Machine-readable error category (e.g., INVALID_ORDER_ID_FORMAT).",
    )
    message: str = Field(
        ...,
        description="Human-readable explanation of why the tool call could not complete.",
    )
    recovery_instructions: str = Field(
        ...,
        description="Actionable step-by-step guidance telling the LLM how to recover.",
    )
    retryable: bool = Field(
        ...,
        description="True if the LLM can retry after correcting arguments or calling another tool.",
    )


# --- Order Domain Schemas ---


class OrderLineItemSchema(StrictSchemaModel):
    """Single SKU line item belonging to an e-commerce order."""

    sku: str = Field(..., description="Unique product SKU identifier.")
    product_name: str = Field(..., description="Display name of the purchased item.")
    category: str = Field(..., description="Product merchandising category.")
    quantity: int = Field(..., ge=1, description="Units purchased.")
    unit_price_usd: float = Field(..., ge=0.0, description="Per-unit purchase price in USD.")
    is_final_sale: bool = Field(
        default=False,
        description="Whether the item was sold under non-returnable clearance terms.",
    )


class OrderLookupInput(StrictSchemaModel):
    """Strict input schema for looking up a single e-commerce order."""

    order_id: str = Field(
        ...,
        min_length=4,
        max_length=20,
        description="Order identifier in 'ORD-XXXX' format (e.g., 'ORD-1001').",
    )


class OrderDetailsOutput(StrictSchemaModel):
    """Strict output schema describing an e-commerce order's full status."""

    status: Literal["success"] = "success"
    order_id: str
    customer_id: str
    order_status: Literal[
        "processing", "shipped", "delivered", "cancelled", "refunded"
    ]
    order_date: str
    delivery_date: str | None = None
    days_since_delivery: int | None = None
    total_amount_usd: float = Field(..., ge=0.0)
    refunded_amount_usd: float = Field(default=0.0, ge=0.0)
    shipping_address_redacted: str
    tracking_number: str | None = None
    carrier: str | None = None
    reported_damaged: bool = False
    items: list[OrderLineItemSchema]


class CustomerOrderListInput(StrictSchemaModel):
    """Strict input schema for listing recent orders belonging to a customer."""

    customer_id: str = Field(
        ...,
        min_length=4,
        max_length=32,
        description="Customer identifier in 'CUST-XXX' format (e.g., 'CUST-101').",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of recent orders to return.",
    )


class CustomerOrderSummary(StrictSchemaModel):
    """Compact summary of a customer order."""

    order_id: str
    order_status: str
    order_date: str
    total_amount_usd: float
    primary_item: str


class CustomerOrderListOutput(StrictSchemaModel):
    """Strict output schema for a customer's recent order list."""

    status: Literal["success"] = "success"
    customer_id: str
    order_count: int
    orders: list[CustomerOrderSummary]


# --- Policy Knowledge Base Schemas ---


class SupportPolicySearchInput(StrictSchemaModel):
    """Strict input schema for semantic search over e-commerce support policies."""

    query: str = Field(
        ...,
        min_length=3,
        max_length=300,
        description="Natural-language policy or troubleshooting search query.",
    )
    top_k: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Number of top matching policy documents to retrieve.",
    )


class PolicyMatchSchema(StrictSchemaModel):
    """Single policy document retrieved from the vector store."""

    policy_id: str
    title: str
    category: str
    similarity_score: float
    content: str


class SupportPolicySearchOutput(StrictSchemaModel):
    """Strict output schema for policy vector search results."""

    status: Literal["success"] = "success"
    query: str
    matches: list[PolicyMatchSchema]


# --- Customer Long-Term Memory Schemas ---


class CustomerMemoryQueryInput(StrictSchemaModel):
    """Strict input schema for recalling long-term customer interaction history."""

    customer_id: str = Field(
        ...,
        min_length=4,
        max_length=32,
        description="Customer identifier (e.g., 'CUST-101').",
    )
    topic_query: str = Field(
        default="recent support issues and preferences",
        min_length=2,
        max_length=200,
        description="Semantic topic to retrieve relevant past memories for.",
    )


class CustomerMemoryEntrySchema(StrictSchemaModel):
    """Single PII-redacted long-term memory record."""

    memory_id: str
    created_at: str
    summary: str
    relevance_score: float


class CustomerMemoryRecallOutput(StrictSchemaModel):
    """Strict output schema for customer long-term memory retrieval."""

    status: Literal["success"] = "success"
    customer_id: str
    memories: list[CustomerMemoryEntrySchema]


# --- Order Mutation, Refund Eligibility, Refund Execution & Escalation Schemas ---


class ShippingAddressUpdateInput(StrictSchemaModel):
    """Strict input schema for updating the destination address on an unshipped order."""

    order_id: str = Field(..., description="Order ID in 'ORD-XXXX' format.")
    new_city_state_zip: str = Field(
        ...,
        min_length=5,
        max_length=120,
        description="Updated city, state, and postal code (e.g., 'Austin, TX 78701').",
    )


class ShippingAddressUpdateOutput(StrictSchemaModel):
    """Strict output schema confirming an order shipping destination update."""

    status: Literal["success"] = "success"
    order_id: str
    previous_destination: str
    updated_destination: str
    confirmation_note: str


class OrderCancellationInput(StrictSchemaModel):
    """Strict input schema for cancelling an unshipped customer order."""

    order_id: str = Field(..., description="Order ID in 'ORD-XXXX' format.")
    cancellation_reason: str = Field(
        ...,
        min_length=5,
        max_length=250,
        description="Customer's stated reason for cancelling the order.",
    )


class OrderCancellationOutput(StrictSchemaModel):
    """Strict output schema confirming order cancellation."""

    status: Literal["success"] = "success"
    order_id: str
    order_status: Literal["cancelled"] = "cancelled"
    released_authorization_usd: float
    confirmation_message: str


class RefundEligibilityInput(StrictSchemaModel):
    """Strict input schema for deterministic + policy-grounded refund eligibility evaluation."""

    order_id: str = Field(..., description="Order ID in 'ORD-XXXX' format.")
    reason_category: Literal[
        "damaged_or_defective",
        "wrong_item_received",
        "changed_mind",
        "late_delivery",
    ] = Field(..., description="Categorized reason for the customer's refund request.")


class RefundEligibilityOutput(StrictSchemaModel):
    """Strict output schema detailing whether an order qualifies for a refund."""

    status: Literal["success"] = "success"
    order_id: str
    eligible: bool
    max_refundable_usd: float = Field(..., ge=0.0)
    requires_human_supervisor_approval: bool
    policy_rule_applied: str
    recommended_next_action: str


class VerifiedOrderRefundInput(StrictSchemaModel):
    """Strict input schema for issuing a verified customer refund."""

    order_id: str = Field(..., description="Order ID in 'ORD-XXXX' format.")
    refund_amount_usd: float = Field(
        ...,
        gt=0.0,
        le=10000.0,
        description="Verified USD amount to refund back to the original payment method.",
    )
    reason: str = Field(
        ...,
        min_length=5,
        max_length=300,
        description="Documented justification for the refund audit ledger.",
    )


class VerifiedOrderRefundOutput(StrictSchemaModel):
    """Strict output schema returned when a refund transaction is recorded."""

    status: Literal["success"] = "success"
    refund_id: str
    order_id: str
    refunded_amount_usd: float
    order_status: str
    supervisor_approved: bool
    settlement_eta_business_days: int = 3


class PriorityEscalationTicketInput(StrictSchemaModel):
    """Strict input schema for creating a Tier-2 human support escalation ticket."""

    order_id: str = Field(..., description="Associated order ID in 'ORD-XXXX' format.")
    customer_id: str = Field(..., description="Customer ID in 'CUST-XXX' format.")
    severity: Literal["low", "medium", "high", "critical"] = Field(
        ..., description="Ticket SLA priority level."
    )
    issue_summary: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description="PII-safe summary of the unresolved dispute or exception request.",
    )


class PriorityEscalationTicketOutput(StrictSchemaModel):
    """Strict output schema returned upon creating an escalation ticket."""

    status: Literal["success"] = "success"
    ticket_id: str
    order_id: str
    customer_id: str
    severity: str
    sla_response_hours: int
    assigned_queue: str
