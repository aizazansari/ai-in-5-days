"""Persistent Session State, Vector Store Knowledge Base, and Async Memory Pipeline.

Implements:
1. Persistent Session State via ADK ``DatabaseSessionService`` (SQLite locally or
   Cloud SQL Postgres in production) or ``VertexAiSessionService``.
2. Persistent Vector Store via ChromaDB (``PersistentClient``) with deterministic
   feature-hashing embeddings (zero network dependency for offline/CI reliability)
   plus Vertex AI Memory Bank (``VertexAiMemoryBankService``) support in GCP.
3. Persistent SQLite E-Commerce Transactional Store (orders, refunds, tickets,
   and PII-scrubbed customer memories) seeded with realistic order scenarios.
4. Non-blocking ``AsyncMemoryConsolidator`` using ``asyncio.create_task`` so
   expensive memory consolidation and vector indexing never block user responses.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid
from typing import Any

from google.adk.memory import BaseMemoryService, InMemoryMemoryService
from google.adk.sessions import BaseSessionService, DatabaseSessionService

from .config import get_settings
from .observability import log_structured_event, tracer
from .pii import pii_redactor

try:
    from google.adk.memory import VertexAiMemoryBankService
    from google.adk.sessions import VertexAiSessionService
except ImportError:  # pragma: no cover
    VertexAiMemoryBankService = None  # type: ignore[assignment,misc]
    VertexAiSessionService = None  # type: ignore[assignment,misc]

try:
    import chromadb
except ImportError:  # pragma: no cover
    chromadb = None  # type: ignore[assignment]


EMBEDDING_DIM = 64

SEED_POLICIES: list[dict[str, str]] = [
    {
        "policy_id": "POL-RETURN-30D",
        "title": "Standard 30-Day Return & Refund Window",
        "category": "returns",
        "content": (
            "Delivered items in original condition are eligible for a full refund within "
            "30 calendar days of the delivery date. Clearance items marked is_final_sale=True "
            "cannot be returned for buyer's remorse ('changed_mind')."
        ),
    },
    {
        "policy_id": "POL-DAMAGED-60D",
        "title": "Damaged, Defective, or Wrong Item Guarantee",
        "category": "damaged_items",
        "content": (
            "If an item arrives damaged, defective, or incorrect, ShopGuard waives return "
            "shipping fees and allows full refunds or replacements up to 60 days post-delivery, "
            "even on final-sale items."
        ),
    },
    {
        "policy_id": "POL-HIGH-VALUE-HITL",
        "title": "Supervisor Approval Threshold for High-Value Refunds",
        "category": "compliance",
        "content": (
            "Refunds up to $100.00 USD are processed automatically once eligibility is verified. "
            "Any single refund exceeding $100.00 USD requires Human-in-the-Loop supervisor "
            "confirmation before funds are disbursed."
        ),
    },
    {
        "policy_id": "POL-SHIPPING-MOD",
        "title": "Order Cancellation & Shipping Address Modification Rules",
        "category": "shipping",
        "content": (
            "Shipping destination updates and order cancellations are only permitted while "
            "an order is in 'processing' (unshipped) status. Once 'shipped' or 'delivered', "
            "carriers cannot reroute packages; offer a post-delivery return or Tier-2 escalation."
        ),
    },
    {
        "policy_id": "POL-LATE-DELIVERY",
        "title": "In-Transit Delays & Carrier Investigation Policy",
        "category": "shipping",
        "content": (
            "Orders currently in 'shipped' status cannot be refunded while actively moving in "
            "transit unless lost for more than 10 business days. Create a priority escalation "
            "ticket for carrier trace investigations."
        ),
    },
]

SEED_ORDERS: list[dict[str, Any]] = [
    {
        "order_id": "ORD-1001",
        "customer_id": "CUST-101",
        "order_status": "delivered",
        "order_date": "2026-10-15",
        "delivery_date": "2026-10-19",
        "days_since_delivery": 10,
        "total_amount_usd": 79.99,
        "refunded_amount_usd": 0.0,
        "shipping_destination": "Seattle, WA 98101",
        "tracking_number": "1Z999AA10123456784",
        "carrier": "UPS",
        "reported_damaged": 0,
        "items": [
            {
                "sku": "SKU-ANC-100",
                "product_name": "AeroPulse Wireless ANC Headphones",
                "category": "electronics",
                "quantity": 1,
                "unit_price_usd": 79.99,
                "is_final_sale": False,
            }
        ],
    },
    {
        "order_id": "ORD-1002",
        "customer_id": "CUST-101",
        "order_status": "shipped",
        "order_date": "2026-10-25",
        "delivery_date": None,
        "days_since_delivery": None,
        "total_amount_usd": 249.00,
        "refunded_amount_usd": 0.0,
        "shipping_destination": "Seattle, WA 98101",
        "tracking_number": "9405511899223456789012",
        "carrier": "USPS",
        "reported_damaged": 0,
        "items": [
            {
                "sku": "SKU-ESP-500",
                "product_name": "BaristaPro Precision Espresso Machine",
                "category": "kitchen",
                "quantity": 1,
                "unit_price_usd": 249.00,
                "is_final_sale": False,
            }
        ],
    },
    {
        "order_id": "ORD-1003",
        "customer_id": "CUST-102",
        "order_status": "processing",
        "order_date": "2026-10-28",
        "delivery_date": None,
        "days_since_delivery": None,
        "total_amount_usd": 389.50,
        "refunded_amount_usd": 0.0,
        "shipping_destination": "Denver, CO 80202",
        "tracking_number": None,
        "carrier": "FedEx",
        "reported_damaged": 0,
        "items": [
            {
                "sku": "SKU-MON-4K27",
                "product_name": "UltraSharp 27-inch 4K USB-C Monitor",
                "category": "electronics",
                "quantity": 1,
                "unit_price_usd": 389.50,
                "is_final_sale": False,
            }
        ],
    },
    {
        "order_id": "ORD-1004",
        "customer_id": "CUST-102",
        "order_status": "delivered",
        "order_date": "2026-10-10",
        "delivery_date": "2026-10-14",
        "days_since_delivery": 15,
        "total_amount_usd": 229.00,
        "refunded_amount_usd": 0.0,
        "shipping_destination": "Denver, CO 80202",
        "tracking_number": "784920194827",
        "carrier": "FedEx",
        "reported_damaged": 1,
        "items": [
            {
                "sku": "SKU-WCH-TITAN",
                "product_name": "ChronoFit Titanium GPS Smartwatch",
                "category": "wearables",
                "quantity": 1,
                "unit_price_usd": 229.00,
                "is_final_sale": False,
            }
        ],
    },
    {
        "order_id": "ORD-1005",
        "customer_id": "CUST-101",
        "order_status": "delivered",
        "order_date": "2026-08-15",
        "delivery_date": "2026-08-20",
        "days_since_delivery": 70,
        "total_amount_usd": 45.00,
        "refunded_amount_usd": 0.0,
        "shipping_destination": "Seattle, WA 98101",
        "tracking_number": "1Z999AA10987654321",
        "carrier": "UPS",
        "reported_damaged": 0,
        "items": [
            {
                "sku": "SKU-BAG-CLR",
                "product_name": "Metro Commuter Sling Bag (Clearance)",
                "category": "accessories",
                "quantity": 1,
                "unit_price_usd": 45.00,
                "is_final_sale": True,
            }
        ],
    },
]

SEED_CUSTOMER_MEMORIES: list[dict[str, str]] = [
    {
        "memory_id": "MEM-SEED-101",
        "customer_id": "CUST-101",
        "created_at": "2026-10-01T12:00:00Z",
        "summary": (
            "Customer CUST-101 is a loyal Plus member who prefers concise updates "
            "and UPS drop-off return labels when returning electronics."
        ),
    },
    {
        "memory_id": "MEM-SEED-102",
        "customer_id": "CUST-102",
        "created_at": "2026-10-16T09:30:00Z",
        "summary": (
            "Customer CUST-102 previously reported carrier box damage on smartwatch "
            "order ORD-1004 upon delivery in Denver."
        ),
    },
]


def compute_text_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Generate a normalized deterministic embedding vector using token/bigram hashing.

    Enables fast, reproducible cosine similarity retrieval in ChromaDB and SQLite
    without blocking on external network calls during CI or offline runs.
    """
    vec = [0.0] * dim
    tokens = [
        tok.strip(".,!?;:()[]\"'").lower()
        for tok in text.split()
        if len(tok.strip(".,!?;:()[]\"'")) >= 2
    ]
    features = list(tokens) + [
        f"{tokens[i]}_{tokens[i + 1]}" for i in range(len(tokens) - 1)
    ]
    if not features:
        vec[0] = 1.0
        return vec

    for feat in features:
        digest = hashlib.sha256(feat.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:2], "big") % dim
        sign = 1.0 if digest[2] % 2 == 0 else -1.0
        vec[bucket] += sign

    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


class PersistentEcommerceStore:
    """SQLite-backed persistent database for e-commerce orders, refunds, tickets, and vector fallback."""

    def __init__(self, db_path: str | None = None, chroma_dir: str | None = None) -> None:
        settings = get_settings()
        self.db_path = db_path or settings.ecommerce_store_db_path
        self.chroma_dir = chroma_dir or settings.chroma_persist_dir
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.chroma_dir).mkdir(parents=True, exist_ok=True)

        self._chroma_policy_collection = None
        self._chroma_memory_collection = None
        self._init_sqlite_schema_and_seed()
        self._init_chroma_collections()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sqlite_schema_and_seed(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    order_status TEXT NOT NULL,
                    order_date TEXT NOT NULL,
                    delivery_date TEXT,
                    days_since_delivery INTEGER,
                    total_amount_usd REAL NOT NULL,
                    refunded_amount_usd REAL NOT NULL DEFAULT 0.0,
                    shipping_destination TEXT NOT NULL,
                    tracking_number TEXT,
                    carrier TEXT,
                    reported_damaged INTEGER NOT NULL DEFAULT 0,
                    items_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS refunds (
                    refund_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    refunded_amount_usd REAL NOT NULL,
                    reason TEXT NOT NULL,
                    supervisor_approved INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS escalation_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    issue_summary TEXT NOT NULL,
                    assigned_queue TEXT NOT NULL,
                    sla_response_hours INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS support_policies (
                    policy_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS customer_memories (
                    memory_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    embedding_json TEXT NOT NULL
                );
                """
            )

            existing_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            if existing_orders == 0:
                for order in SEED_ORDERS:
                    conn.execute(
                        """
                        INSERT INTO orders (
                            order_id, customer_id, order_status, order_date,
                            delivery_date, days_since_delivery, total_amount_usd,
                            refunded_amount_usd, shipping_destination,
                            tracking_number, carrier, reported_damaged, items_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            order["order_id"],
                            order["customer_id"],
                            order["order_status"],
                            order["order_date"],
                            order["delivery_date"],
                            order["days_since_delivery"],
                            order["total_amount_usd"],
                            order["refunded_amount_usd"],
                            order["shipping_destination"],
                            order["tracking_number"],
                            order["carrier"],
                            order["reported_damaged"],
                            json.dumps(order["items"]),
                        ),
                    )

            existing_policies = conn.execute(
                "SELECT COUNT(*) FROM support_policies"
            ).fetchone()[0]
            if existing_policies == 0:
                for pol in SEED_POLICIES:
                    emb = compute_text_embedding(
                        f"{pol['title']} {pol['category']} {pol['content']}"
                    )
                    conn.execute(
                        """
                        INSERT INTO support_policies (
                            policy_id, title, category, content, embedding_json
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            pol["policy_id"],
                            pol["title"],
                            pol["category"],
                            pol["content"],
                            json.dumps(emb),
                        ),
                    )

            existing_memories = conn.execute(
                "SELECT COUNT(*) FROM customer_memories"
            ).fetchone()[0]
            if existing_memories == 0:
                for mem in SEED_CUSTOMER_MEMORIES:
                    clean_summary = pii_redactor.scrub_text(mem["summary"])
                    emb = compute_text_embedding(clean_summary)
                    conn.execute(
                        """
                        INSERT INTO customer_memories (
                            memory_id, customer_id, created_at, summary, embedding_json
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            mem["memory_id"],
                            mem["customer_id"],
                            mem["created_at"],
                            clean_summary,
                            json.dumps(emb),
                        ),
                    )

    def _init_chroma_collections(self) -> None:
        """Initialize persistent ChromaDB vector collections when chromadb is installed."""
        if chromadb is None:
            return
        try:
            client = chromadb.PersistentClient(path=self.chroma_dir)
            self._chroma_policy_collection = client.get_or_create_collection(
                name="ecommerce_support_policies",
                metadata={"hnsw:space": "cosine"},
            )
            self._chroma_memory_collection = client.get_or_create_collection(
                name="customer_long_term_memories",
                metadata={"hnsw:space": "cosine"},
            )

            if self._chroma_policy_collection.count() == 0:
                self._chroma_policy_collection.upsert(
                    ids=[p["policy_id"] for p in SEED_POLICIES],
                    documents=[p["content"] for p in SEED_POLICIES],
                    embeddings=[
                        compute_text_embedding(
                            f"{p['title']} {p['category']} {p['content']}"
                        )
                        for p in SEED_POLICIES
                    ],
                    metadatas=[
                        {"title": p["title"], "category": p["category"]}
                        for p in SEED_POLICIES
                    ],
                )

            if self._chroma_memory_collection.count() == 0:
                self._chroma_memory_collection.upsert(
                    ids=[m["memory_id"] for m in SEED_CUSTOMER_MEMORIES],
                    documents=[
                        pii_redactor.scrub_text(m["summary"])
                        for m in SEED_CUSTOMER_MEMORIES
                    ],
                    embeddings=[
                        compute_text_embedding(pii_redactor.scrub_text(m["summary"]))
                        for m in SEED_CUSTOMER_MEMORIES
                    ],
                    metadatas=[
                        {
                            "customer_id": m["customer_id"],
                            "created_at": m["created_at"],
                        }
                        for m in SEED_CUSTOMER_MEMORIES
                    ],
                )
        except Exception:
            self._chroma_policy_collection = None
            self._chroma_memory_collection = None

    # --- Order Queries & Mutations ---

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            record["items"] = json.loads(record.pop("items_json"))
            record["reported_damaged"] = bool(record["reported_damaged"])
            return record

    def list_customer_orders(
        self, customer_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE customer_id = ?
                ORDER BY order_date DESC
                LIMIT ?
                """,
                (customer_id, limit),
            ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                data = dict(row)
                data["items"] = json.loads(data.pop("items_json"))
                results.append(data)
            return results

    def update_shipping_destination(
        self, order_id: str, new_destination: str
    ) -> dict[str, Any]:
        clean_dest = pii_redactor.scrub_text(new_destination)
        with self._connect() as conn:
            conn.execute(
                "UPDATE orders SET shipping_destination = ? WHERE order_id = ?",
                (clean_dest, order_id),
            )
        return self.get_order(order_id) or {}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "UPDATE orders SET order_status = 'cancelled' WHERE order_id = ?",
                (order_id,),
            )
        return self.get_order(order_id) or {}

    def record_refund(
        self,
        *,
        order_id: str,
        refund_amount_usd: float,
        reason: str,
        supervisor_approved: bool,
    ) -> dict[str, Any]:
        refund_id = f"RFND-{uuid.uuid4().hex[:8].upper()}"
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_reason = pii_redactor.scrub_text(reason)

        with self._connect() as conn:
            order_row = conn.execute(
                "SELECT total_amount_usd, refunded_amount_usd FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            new_refunded = round(
                float(order_row["refunded_amount_usd"]) + refund_amount_usd, 2
            )
            new_status = (
                "refunded"
                if new_refunded >= float(order_row["total_amount_usd"])
                else "delivered"
            )
            conn.execute(
                """
                UPDATE orders
                SET refunded_amount_usd = ?, order_status = ?
                WHERE order_id = ?
                """,
                (new_refunded, new_status, order_id),
            )
            conn.execute(
                """
                INSERT INTO refunds (
                    refund_id, order_id, refunded_amount_usd, reason,
                    supervisor_approved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    refund_id,
                    order_id,
                    refund_amount_usd,
                    clean_reason,
                    int(supervisor_approved),
                    now_iso,
                ),
            )
        return {
            "refund_id": refund_id,
            "order_id": order_id,
            "refunded_amount_usd": refund_amount_usd,
            "order_status": new_status,
            "supervisor_approved": supervisor_approved,
        }

    def create_escalation_ticket(
        self,
        *,
        order_id: str,
        customer_id: str,
        severity: str,
        issue_summary: str,
    ) -> dict[str, Any]:
        ticket_id = f"ESC-{uuid.uuid4().hex[:8].upper()}"
        sla_map = {"critical": 1, "high": 4, "medium": 12, "low": 24}
        queue_map = {
            "critical": "tier2-executive-escalations",
            "high": "tier2-senior-resolutions",
            "medium": "tier2-order-exceptions",
            "low": "tier2-general-review",
        }
        sla_hours = sla_map.get(severity, 12)
        queue = queue_map.get(severity, "tier2-order-exceptions")
        clean_summary = pii_redactor.scrub_text(issue_summary)
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO escalation_tickets (
                    ticket_id, order_id, customer_id, severity,
                    issue_summary, assigned_queue, sla_response_hours, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    order_id,
                    customer_id,
                    severity,
                    clean_summary,
                    queue,
                    sla_hours,
                    now_iso,
                ),
            )
        return {
            "ticket_id": ticket_id,
            "order_id": order_id,
            "customer_id": customer_id,
            "severity": severity,
            "sla_response_hours": sla_hours,
            "assigned_queue": queue,
        }

    # --- Vector Search: Policies & Customer Long-Term Memory ---

    def search_policies(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        query_emb = compute_text_embedding(query)

        if self._chroma_policy_collection is not None:
            try:
                res = self._chroma_policy_collection.query(
                    query_embeddings=[query_emb],
                    n_results=min(top_k, self._chroma_policy_collection.count()),
                )
                ids = res.get("ids", [[]])[0]
                docs = res.get("documents", [[]])[0]
                metas = res.get("metadatas", [[]])[0]
                dists = res.get("distances", [[]])[0]
                return [
                    {
                        "policy_id": pid,
                        "title": str(meta.get("title", "")),
                        "category": str(meta.get("category", "")),
                        "similarity_score": round(max(0.0, 1.0 - float(dist)), 4),
                        "content": doc,
                    }
                    for pid, doc, meta, dist in zip(ids, docs, metas, dists)
                ]
            except Exception:
                pass

        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM support_policies").fetchall()
            scored = []
            for row in rows:
                emb = json.loads(row["embedding_json"])
                sim = _cosine_similarity(query_emb, emb)
                scored.append(
                    {
                        "policy_id": row["policy_id"],
                        "title": row["title"],
                        "category": row["category"],
                        "similarity_score": round(max(0.0, sim), 4),
                        "content": row["content"],
                    }
                )
            scored.sort(key=lambda item: item["similarity_score"], reverse=True)
            return scored[:top_k]

    def store_customer_memory(self, customer_id: str, summary: str) -> dict[str, Any]:
        """Persist a PII-scrubbed customer memory into SQLite and ChromaDB."""
        clean_summary = pii_redactor.scrub_text(summary)
        memory_id = f"MEM-{uuid.uuid4().hex[:8].upper()}"
        created_at = datetime.now(timezone.utc).isoformat()
        emb = compute_text_embedding(clean_summary)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO customer_memories (
                    memory_id, customer_id, created_at, summary, embedding_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (memory_id, customer_id, created_at, clean_summary, json.dumps(emb)),
            )

        if self._chroma_memory_collection is not None:
            try:
                self._chroma_memory_collection.upsert(
                    ids=[memory_id],
                    documents=[clean_summary],
                    embeddings=[emb],
                    metadatas=[{"customer_id": customer_id, "created_at": created_at}],
                )
            except Exception:
                pass

        return {
            "memory_id": memory_id,
            "customer_id": customer_id,
            "created_at": created_at,
            "summary": clean_summary,
        }

    def search_customer_memories(
        self, customer_id: str, topic_query: str, top_k: int = 5
    ) -> list[dict[str, Any]]:
        query_emb = compute_text_embedding(topic_query)

        if self._chroma_memory_collection is not None:
            try:
                res = self._chroma_memory_collection.query(
                    query_embeddings=[query_emb],
                    n_results=top_k,
                    where={"customer_id": customer_id},
                )
                ids = res.get("ids", [[]])[0]
                docs = res.get("documents", [[]])[0]
                metas = res.get("metadatas", [[]])[0]
                dists = res.get("distances", [[]])[0]
                if ids:
                    return [
                        {
                            "memory_id": mid,
                            "created_at": str(meta.get("created_at", "")),
                            "summary": doc,
                            "relevance_score": round(max(0.0, 1.0 - float(dist)), 4),
                        }
                        for mid, doc, meta, dist in zip(ids, docs, metas, dists)
                    ]
            except Exception:
                pass

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM customer_memories WHERE customer_id = ?",
                (customer_id,),
            ).fetchall()
            scored = []
            for row in rows:
                emb = json.loads(row["embedding_json"])
                sim = _cosine_similarity(query_emb, emb)
                scored.append(
                    {
                        "memory_id": row["memory_id"],
                        "created_at": row["created_at"],
                        "summary": row["summary"],
                        "relevance_score": round(max(0.0, sim), 4),
                    }
                )
            scored.sort(key=lambda item: item["relevance_score"], reverse=True)
            return scored[:top_k]


class AsyncMemoryConsolidator:
    """Executes expensive memory scrubbing, embedding, and Memory Bank sync asynchronously.

    Prevents UI / turn response blocking by dispatching consolidation tasks onto
    the running asyncio event loop in the background via ``asyncio.create_task``.
    """

    def __init__(
        self,
        store: PersistentEcommerceStore,
        memory_service: BaseMemoryService | None = None,
    ) -> None:
        self.store = store
        self.memory_service = memory_service
        self._pending_tasks: set[asyncio.Task[Any]] = set()

    def schedule_consolidation(
        self,
        *,
        customer_id: str,
        interaction_summary: str,
        session: Any | None = None,
    ) -> asyncio.Task[dict[str, Any]] | None:
        """Fire-and-forget non-blocking background memory consolidation."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # If invoked synchronously outside an active event loop (e.g., sync CLI test),
            # run via asyncio.run without losing the memory update.
            asyncio.run(
                self.consolidate_async(
                    customer_id=customer_id,
                    interaction_summary=interaction_summary,
                    session=session,
                )
            )
            return None

        task = loop.create_task(
            self.consolidate_async(
                customer_id=customer_id,
                interaction_summary=interaction_summary,
                session=session,
            ),
            name=f"consolidate_memory_{customer_id}",
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    async def consolidate_async(
        self,
        *,
        customer_id: str,
        interaction_summary: str,
        session: Any | None = None,
    ) -> dict[str, Any]:
        """Asynchronously scrub PII, compute vector embeddings, and persist memory."""
        with tracer.start_as_current_span("memory.async_consolidation") as span:
            span.set_attribute("customer.id", customer_id)
            clean_summary = pii_redactor.scrub_text(interaction_summary)

            log_structured_event(
                "Starting async background memory consolidation",
                event_type="MEMORY_CONSOLIDATION",
                event_phase="INTENT",
                intent_payload={
                    "customer_id": customer_id,
                    "summary_preview": clean_summary[:120],
                },
            )

            # Offload synchronous SQLite/Chroma disk I/O to worker thread so event loop never stalls
            saved_entry = await asyncio.to_thread(
                self.store.store_customer_memory,
                customer_id,
                clean_summary,
            )

            # Also synchronize completed session to ADK MemoryService (e.g., VertexAiMemoryBankService)
            if self.memory_service is not None and session is not None:
                try:
                    await self.memory_service.add_session_to_memory(session)
                except Exception:
                    pass

            log_structured_event(
                "Completed async background memory consolidation",
                event_type="MEMORY_CONSOLIDATION",
                event_phase="OUTCOME",
                outcome_payload={
                    "memory_id": saved_entry["memory_id"],
                    "customer_id": customer_id,
                },
            )
            return saved_entry

    async def flush_pending_tasks(self) -> None:
        """Await any in-flight background memory tasks (useful in tests & graceful shutdown)."""
        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)


def create_session_service() -> BaseSessionService:
    """Create persistent ADK SessionService (Vertex AI Agent Engine or SQLite/Postgres DB)."""
    settings = get_settings()
    if (
        settings.memory_backend == "vertex"
        and settings.google_cloud_project
        and settings.vertex_agent_engine_id
        and VertexAiSessionService is not None
    ):
        return VertexAiSessionService(
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
            agent_engine_id=settings.vertex_agent_engine_id,
        )
    return DatabaseSessionService(db_url=settings.session_db_url)


def create_memory_service() -> BaseMemoryService:
    """Create ADK MemoryService (VertexAiMemoryBankService in cloud or local service)."""
    settings = get_settings()
    if (
        settings.memory_backend == "vertex"
        and settings.google_cloud_project
        and settings.vertex_agent_engine_id
        and VertexAiMemoryBankService is not None
    ):
        return VertexAiMemoryBankService(
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
            agent_engine_id=settings.vertex_agent_engine_id,
        )
    return InMemoryMemoryService()


# Shared persistent e-commerce store & non-blocking async memory consolidator
ecommerce_store = PersistentEcommerceStore()
adk_memory_service = create_memory_service()
async_memory_consolidator = AsyncMemoryConsolidator(
    store=ecommerce_store,
    memory_service=adk_memory_service,
)
