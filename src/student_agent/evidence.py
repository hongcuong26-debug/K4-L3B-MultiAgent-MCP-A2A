from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import httpx2

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

PERMISSIONS = {
    "entity-agent": {"get_customer_history", "get_order"},
    "order-agent": {"get_order_items", "get_product_context", "get_sellers"},
    "payment-agent": {"get_payment_timeline", "get_refund_timeline"},
    "shipment-agent": {"get_shipment_summary"},
    "policy-agent": {"get_policy"},
}
DOMAINS = {
    "get_order": "order",
    "get_customer_history": "customer",
    "get_order_items": "item",
    "get_product_context": "product",
    "get_sellers": "seller",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_policy": "policy",
}


class EvidenceCollector:
    """A case-local broker: authorization, discovery, bounded retry and receipts."""

    def __init__(self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.cache: dict[str, dict[str, Any] | None] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.calls = 0
        self.max_calls = 16
        self.timeout = 25.0
        self.retry_delay = 0.25
        self.failures: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def event(self, event_type: str, actor: str, **kwargs: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **kwargs)

    async def get(self, actor: str, tool: str, **arguments: str) -> dict[str, Any] | None:
        if tool not in PERMISSIONS.get(actor, set()):
            raise PermissionError(f"{actor} is not permitted to use {tool}")
        if tool not in await self.gateway.list_tools():
            self.failures.add(tool)
            self.event(
                "handoff",
                actor,
                target="coordinator",
                decision_code="TOOL_UNAVAILABLE",
                tool_name=tool,
            )
            return None
        key = json.dumps([tool, arguments], sort_keys=True)
        # The lock also coalesces duplicate simultaneous requests without extra audited calls.
        async with self._locks.setdefault(key, asyncio.Lock()):
            if key in self.cache:
                result = self.cache[key]
            else:
                result = await self._fetch(actor, tool, arguments)
                self.cache[key] = result
        if result is not None:
            self.event(
                "tool_result_consumed",
                actor,
                tool_name=tool,
                evidence_refs=[result["evidence_ref"]],
            )
        return copy.deepcopy(result)

    async def _fetch(
        self, actor: str, tool: str, arguments: dict[str, str]
    ) -> dict[str, Any] | None:
        for attempt in range(2):
            if self.calls >= self.max_calls:
                self.failures.add(tool)
                self.event(
                    "handoff",
                    actor,
                    target="coordinator",
                    tool_name=tool,
                    decision_code="CALL_BUDGET_EXHAUSTED",
                )
                return None
            self.calls += 1
            try:
                async with asyncio.timeout(self.timeout):
                    result = await self.gateway.call(tool, case_id=self.case_id, **arguments)
                self.trace.contracts.validate_evidence(result)
                if result["domain"] != DOMAINS[tool]:
                    raise ValueError("Evidence domain does not match the requested tool")
                self._check_scope(result["data"], arguments)
                ref = result["evidence_ref"]
                if ref in self.receipts and self.receipts[ref]["envelope"] != result:
                    raise ValueError("An evidence ref was reused with different content")
                self.receipts[ref] = {"tool": tool, "arguments": arguments, "envelope": result}
                return result
            except (TimeoutError, httpx2.TransportError):
                self.event(
                    "handoff",
                    actor,
                    target="coordinator",
                    tool_name=tool,
                    decision_code="MCP_TIMEOUT" if attempt else "MCP_RETRY",
                    attributes={"attempt": attempt + 1},
                )
                if not attempt:
                    await asyncio.sleep(self.retry_delay)
            except RuntimeError:
                # Semantic tool errors, missing data and authorization failures are not retried.
                self.event(
                    "handoff",
                    actor,
                    target="coordinator",
                    tool_name=tool,
                    decision_code="MCP_TOOL_ERROR",
                )
                break
        self.failures.add(tool)
        return None

    def _check_scope(self, data: Any, arguments: dict[str, str]) -> None:
        if isinstance(data, list):
            for row in data:
                self._check_scope(row, arguments)
        elif isinstance(data, dict):
            if data.get("case_id", self.case_id) != self.case_id:
                raise ValueError("Cross-case evidence")
            for field, value in arguments.items():
                if field in data and data[field] != value:
                    raise ValueError("Cross-entity evidence")
            for value in data.values():
                if isinstance(value, (list, dict)):
                    self._check_scope(value, arguments)

    def refs(self, *domains: str) -> list[str]:
        return [
            ref
            for ref, receipt in self.receipts.items()
            if not domains or receipt["envelope"]["domain"] in domains
        ]


def data_of(evidence: dict[str, Any] | None, default: Any) -> Any:
    return evidence["data"] if evidence is not None else default
