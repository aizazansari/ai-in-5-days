"""Automated Golden Dataset Regression & ADK AgentEvaluator Test Harness."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from google.adk.evaluation.agent_evaluator import AgentEvaluator
from google.adk.evaluation.eval_set import EvalSet
import pytest

from app.agent import app
from app.memory import AsyncMemoryConsolidator, PersistentEcommerceStore
import app.tools as tools_module

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
GOLDEN_EVALSET_PATH = EVAL_DIR / "ecommerce_order_support.evalset.json"
SMOKE_TEST_PATH = EVAL_DIR / "order_lookup_and_refund.test.json"

TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "lookup_ecommerce_order_by_id": tools_module.lookup_ecommerce_order_by_id,
    "list_recent_customer_orders": tools_module.list_recent_customer_orders,
    "search_ecommerce_return_and_shipping_policies": (
        tools_module.search_ecommerce_return_and_shipping_policies
    ),
    "recall_customer_support_history": tools_module.recall_customer_support_history,
    "update_unshipped_order_shipping_address": (
        tools_module.update_unshipped_order_shipping_address
    ),
    "cancel_unshipped_customer_order": tools_module.cancel_unshipped_customer_order,
    "evaluate_order_refund_eligibility": tools_module.evaluate_order_refund_eligibility,
    "issue_verified_order_refund": tools_module.issue_verified_order_refund,
    "create_priority_support_escalation_ticket": (
        tools_module.create_priority_support_escalation_ticket
    ),
}


def test_golden_datasets_validate_against_adk_evalset_schema() -> None:
    """Statically verify golden dataset files conform to ADK's Pydantic EvalSet schema."""
    for dataset_path in (GOLDEN_EVALSET_PATH, SMOKE_TEST_PATH):
        raw_data = json.loads(dataset_path.read_text(encoding="utf-8"))
        eval_set = EvalSet.model_validate(raw_data)
        assert len(eval_set.eval_cases) >= 1


def test_golden_dataset_tool_trajectories_execute_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay every golden dataset tool-use trajectory against an isolated store to catch regressions."""
    store = PersistentEcommerceStore(
        db_path=str(tmp_path / "golden_eval.db"),
        chroma_dir=str(tmp_path / "golden_chroma"),
    )
    monkeypatch.setattr(tools_module, "ecommerce_store", store)
    monkeypatch.setattr(
        tools_module,
        "async_memory_consolidator",
        AsyncMemoryConsolidator(store=store),
    )

    eval_set = EvalSet.model_validate(
        json.loads(GOLDEN_EVALSET_PATH.read_text(encoding="utf-8"))
    )
    assert len(eval_set.eval_cases) == 5

    for eval_case in eval_set.eval_cases:
        for turn in eval_case.conversation:
            assert turn.intermediate_data is not None
            for tool_call in turn.intermediate_data.tool_uses:
                assert tool_call.name in TOOL_REGISTRY, (
                    f"Unknown tool '{tool_call.name}' in {eval_case.eval_id}"
                )
                fn = TOOL_REGISTRY[tool_call.name]
                result = fn(**(tool_call.args or {}))
                assert result["status"] == "success", (
                    f"Golden trajectory regression in {eval_case.eval_id} "
                    f"calling {tool_call.name}: {result}"
                )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_ADK_EVAL", "false").lower() != "true",
    reason="Set RUN_LIVE_ADK_EVAL=true with GOOGLE_API_KEY / Vertex AI auth to run live LLM evaluation.",
)
async def test_live_adk_agent_evaluator_regression_suite() -> None:
    """Execute full live ADK AgentEvaluator against the golden EvalSet."""
    eval_set = EvalSet.model_validate(
        json.loads(GOLDEN_EVALSET_PATH.read_text(encoding="utf-8"))
    )
    eval_config = AgentEvaluator.find_config_for_test_file(str(GOLDEN_EVALSET_PATH))
    await AgentEvaluator.evaluate_eval_set(
        agent_module=app,
        eval_set=eval_set,
        eval_config=eval_config,
        num_runs=1,
    )
