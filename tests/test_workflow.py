from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from student_agent.analysis import analyze_payment, analyze_shipment
from student_agent.cases import CaseSet
from student_agent.cli import _resume_completed
from student_agent.contracts import Contracts
from student_agent.evidence import DOMAINS, EvidenceCollector
from student_agent.submission import package_submission, validate_artifacts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case, verify_output

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contracts():
    return Contracts(ROOT / "contracts" / "schemas")


@pytest.fixture
def case():
    return {
        "case_id": "CASE_001",
        "policy_version": "POLICY_TEST",
        "candidate_order_ids": ["order-a", "wrong-order"],
        "customer_unique_id_hint": "customer-a",
        "investigation_scope": {"include_product_context": True},
        "customer_request": {
            "claimed_order_id": "order-a",
            "claims": [
                {"claim_id": "claim-a", "topic": "canceled_order_paid"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
    }


class FakeGateway:
    """Synthetic test fixture only; never used by the CLI or submission path."""

    def __init__(self):
        self.calls = []
        self.failures = []
        self.payloads = {
            "get_customer_history": {
                "customer_unique_id": "customer-a",
                "orders": [{"order_id": "order-a", "order_status": "canceled"}],
            },
            "get_order": {"order_id": "order-a", "order_status": "canceled"},
            "get_order_items": [
                {
                    "order_id": "order-a",
                    "order_item_id": "item-a",
                    "seller_id": "seller-a",
                    "price": "79",
                    "freight_value": "10",
                }
            ],
            "get_product_context": [{"product_id": "product-a"}],
            "get_payment_timeline": {
                "order_id": "order-a",
                "payments": [],
                "events": [{"event_type": "captured", "amount_brl": "89", "status": "confirmed"}],
            },
            "get_refund_timeline": {"order_id": "order-a", "events": []},
            "get_shipment_summary": {"order_id": "order-a", "order_status": "canceled"},
            "get_policy": {
                "currency": "BRL",
                "policy_version": "POLICY_TEST",
                "rules": {
                    "canceled_order_paid": {
                        "case_status": "action_required",
                        "refund_brl": 89,
                        "recommended_action": "issue_refund",
                        "responsible_parties": [{"party_type": "platform", "party_id": None}],
                    }
                },
            },
        }

    async def list_tools(self):
        return sorted(self.payloads)

    async def call(self, tool_name, *, case_id, **arguments):
        self.calls.append((tool_name, case_id, arguments))
        if self.failures:
            raise self.failures.pop(0)
        data = copy.deepcopy(self.payloads[tool_name])
        digest = hashlib.sha256(json.dumps([case_id, tool_name, data]).encode()).hexdigest()
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_" + digest,
            "result_hash": "sha256:" + digest,
            "domain": DOMAINS[tool_name],
            "data": data,
        }


def test_case_end_to_end_with_scoped_evidence(tmp_path, contracts, case, monkeypatch):
    trace = TraceWriter(tmp_path / "traces" / "trace.jsonl", contracts)
    gateway = FakeGateway()
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    assert output["financial_resolution"]["recommended_refund_brl"] == 89
    assert output["entity_resolution"]["rejected_candidates"] == ["wrong-order"]
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert all(call[1] == "CASE_001" for call in gateway.calls)
    assert len(gateway.calls) == 7
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "CASE_001.json").write_text(json.dumps(output), encoding="utf-8")
    case_set = CaseSet("test-v1", "l3b", ("CASE_001",), {"CASE_001": case})
    validate_artifacts(tmp_path, case_set, contracts)
    monkeypatch.setattr("student_agent.cases.load_case_set", lambda root: case_set)
    monkeypatch.setattr("student_agent.submission.Contracts", lambda root: contracts)
    (tmp_path / ".env").write_text("not-for-release", encoding="utf-8")
    archive_path = package_submission(tmp_path, tmp_path / "dist" / "submission.zip")
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {"manifest.json", "trace.jsonl", "outputs/CASE_001.json"}
        contracts.validate_manifest(json.loads(archive.read("manifest.json")))
    trace.emit(case_id="CASE_002", event_type="case_received", actor="coordinator")
    assert _resume_completed(tmp_path, ("CASE_001", "CASE_002"), contracts) == {"CASE_001"}
    assert (tmp_path / "traces" / "interrupted-attempts.jsonl").exists()
    validate_artifacts(tmp_path, case_set, contracts)
    trace.path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete observable"):
        validate_artifacts(tmp_path, case_set, contracts)


def test_missing_refund_evidence_never_authorizes_refund(tmp_path, contracts, case):
    gateway = FakeGateway()
    case["customer_request"]["claims"][0]["topic"] = "refund_failed"
    del gateway.payloads["get_refund_timeline"]
    output = asyncio.run(
        solve_case(case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts))
    )
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["payment_analysis"]["refunded_total_brl"] is None
    assert output["assessment"]["case_status"] == "needs_investigation"


def test_ambiguous_candidates_do_not_trigger_order_specialists(tmp_path, contracts, case):
    gateway = FakeGateway()
    case["customer_request"]["claimed_order_id"] = None
    gateway.payloads["get_customer_history"]["orders"].append({"order_id": "wrong-order"})
    output = asyncio.run(
        solve_case(case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts))
    )
    assert output["entity_resolution"]["status"] == "ambiguous"
    assert output["affected_entities"]["order_ids"] == []
    assert len(gateway.calls) == 2


def test_permissions_retry_and_cache(tmp_path, contracts):
    gateway = FakeGateway()
    gateway.failures = [TimeoutError()]
    broker = EvidenceCollector(
        "CASE_001", gateway, TraceWriter(tmp_path / "trace.jsonl", contracts)
    )
    broker.retry_delay = 0

    async def exercise():
        with pytest.raises(PermissionError):
            await broker.get("shipment-agent", "get_policy", policy_version="POLICY_TEST")
        a, b = await asyncio.gather(
            *[broker.get("entity-agent", "get_order", order_id="order-a") for _ in range(2)]
        )
        assert a == b
        assert len(gateway.calls) == 2  # One failed attempt and one success, no duplicate request.
        a["data"]["order_id"] = "tampered"
        assert (await broker.get("entity-agent", "get_order", order_id="order-a"))["data"][
            "order_id"
        ] == "order-a"

    asyncio.run(exercise())


def test_retry_is_bounded_and_tool_errors_are_not_retried(tmp_path, contracts):
    for failures, expected in [([TimeoutError(), TimeoutError()], 2), ([RuntimeError()], 1)]:
        gateway = FakeGateway()
        gateway.failures = failures
        broker = EvidenceCollector(
            "CASE_001", gateway, TraceWriter(tmp_path / "trace.jsonl", contracts)
        )
        broker.retry_delay = 0
        assert asyncio.run(broker.get("entity-agent", "get_order", order_id="order-a")) is None
        assert len(gateway.calls) == expected


def test_nested_cross_entity_evidence_rejected(tmp_path, contracts):
    gateway = FakeGateway()
    gateway.payloads["get_payment_timeline"]["events"][0]["order_id"] = "another-order"
    broker = EvidenceCollector(
        "CASE_001", gateway, TraceWriter(tmp_path / "trace.jsonl", contracts)
    )
    with pytest.raises(ValueError, match="Cross-entity"):
        asyncio.run(broker.get("payment-agent", "get_payment_timeline", order_id="order-a"))
    assert broker.receipts == {}


def test_split_payment_and_refund_completion():
    timeline = {
        "payments": [{"payment_sequential": "1"}, {"payment_sequential": "2"}],
        "events": [
            {"event_type": "captured", "status": "confirmed", "amount_brl": "44.5"},
            {"event_type": "captured", "status": "confirmed", "amount_brl": "44.5"},
        ],
    }
    payment, issues = analyze_payment(timeline, {"events": []}, Decimal(89))
    assert issues == ["valid_split_payment"]
    assert payment["verdict"] == "reconciled"
    refund = {
        "events": [
            {"event_at": "2018-01-01T00:00:00Z", "status": "pending", "amount_brl": 89},
            {"event_at": "2018-01-02T00:00:00Z", "status": "completed", "amount_brl": 89},
        ]
    }
    payment, issues = analyze_payment(timeline, refund, Decimal(89))
    assert payment["verdict"] == "refunded"
    assert payment["refundable_total_brl"] == 0
    assert issues == []


def test_shipping_contradiction_is_not_silently_resolved():
    summary = {
        "delivered_carrier_at": "2018-01-01T00:00:00Z",
        "delivered_customer_at": "2018-01-03T00:00:00Z",
        "estimated_delivery_at": "2018-01-04T00:00:00Z",
        "shipping_limits": [{"seller_id": "seller-a", "shipping_limit_at": "2018-01-02T00:00:00Z"}],
        "events": [
            {"event_type": "delivered_late", "actor": "logistics_provider", "status": "confirmed"}
        ],
    }
    shipping, _, conflicts = analyze_shipment(summary)
    assert shipping["verdict"] == "conflicting"
    assert not shipping["timeline_complete"]
    assert conflicts[0]["selected_source"] is None


def test_verifier_rejects_fabricated_reference(tmp_path, contracts, case):
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = asyncio.run(solve_case(case, FakeGateway(), trace))
    broker = EvidenceCollector("CASE_001", FakeGateway(), trace)
    with pytest.raises(ValueError, match="not collected"):
        verify_output(case, output, broker)


def test_public_schemas_remain_locked():
    lock = json.loads((ROOT / "contracts" / "schema-lock.json").read_text(encoding="utf-8"))
    actual = {
        path.name: hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for path in (ROOT / "contracts" / "schemas").glob("*.schema.json")
    }
    assert actual == lock
