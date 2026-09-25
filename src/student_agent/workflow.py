from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .analysis import analyze_items, analyze_payment, analyze_shipment, ids, money, rows
from .episodes import Episode, scope_payment, scope_shipment, select_episode
from .evidence import EvidenceCollector, data_of
from .mcp_gateway import EvidenceGateway
from .semantic_model import select_primary_issue
from .trace import TraceWriter


async def _resolve(case: dict[str, Any], broker: EvidenceCollector) -> dict[str, Any]:
    broker.event("task_assigned", "coordinator", target="entity-agent")
    hint = case.get("customer_unique_id_hint")
    history = {}
    if hint:
        history = data_of(
            await broker.get("entity-agent", "get_customer_history", customer_unique_id=hint), {}
        )
    candidates = list(dict.fromkeys(case.get("candidate_order_ids", [])))
    claimed = case.get("customer_request", {}).get("claimed_order_id")
    if claimed and claimed not in candidates:
        candidates.append(claimed)
    related = ids(rows(history, "orders"), "order_id")
    matches = [candidate for candidate in candidates if candidate in related]
    if claimed in matches:
        matches = [claimed]
    rejected = [candidate for candidate in candidates if related and candidate not in related]
    order = {}
    if len(matches) == 1:
        order = data_of(await broker.get("entity-agent", "get_order", order_id=matches[0]), {})
    elif not hint and len(candidates) == 1:
        # Exact identity can be corroborated by the authoritative order tool without a hint.
        order = data_of(await broker.get("entity-agent", "get_order", order_id=candidates[0]), {})
        if order.get("order_id") == candidates[0]:
            matches = candidates
    resolved = bool(len(matches) == 1 and order.get("order_id") == matches[0])
    episode = select_episode(case, order, history)
    resolved = resolved and episode.scoped
    result = {
        "resolution": {
            "status": "resolved" if resolved else "ambiguous",
            "resolved_order_ids": matches if resolved else [],
            "rejected_candidates": rejected,
            "confidence": 0.98 if resolved and history else 0.7 if resolved else 0.2,
        },
        "customer": {
            "customer_unique_id": history.get("customer_unique_id"),
            "related_order_ids": related,
        },
        "order": episode.order if resolved else {},
        "direct_order": order,
        "episode": episode,
        "history": history,
    }
    if history and not matches:
        result["resolution"].update(status="not_found", confidence=0.9)
    broker.event(
        "handoff",
        "entity-agent",
        target="coordinator",
        decision_code=result["resolution"]["status"].upper(),
        evidence_refs=broker.refs("customer", "order"),
    )
    return result


async def _order_agent(
    case: dict, order_id: str, broker: EvidenceCollector, episode: Episode
) -> dict:
    items = rows(data_of(await broker.get("order-agent", "get_order_items", order_id=order_id), []))
    items = episode.filter(items, "shipping_limit_date")
    if case.get("investigation_scope", {}).get("include_product_context"):
        await broker.get("order-agent", "get_product_context", order_id=order_id)
    expected, conflicts = analyze_items(items)
    broker.event(
        "handoff",
        "order-agent",
        target="policy-agent",
        evidence_refs=broker.refs("item", "product"),
    )
    return {"items": items, "expected": expected, "conflicts": conflicts}


async def _payment_agent(
    case: dict, order_id: str, broker: EvidenceCollector, episode: Episode
) -> dict:
    timeline = data_of(
        await broker.get("payment-agent", "get_payment_timeline", order_id=order_id), {}
    )
    timeline = scope_payment(timeline, episode)
    topics = {claim.get("topic") for claim in case.get("customer_request", {}).get("claims", [])}
    refund = None
    refund_required = bool(
        topics & {"refund_pending", "refund_failed", "refund_not_received"}
    ) or any("refund" in str(event.get("event_type")) for event in rows(timeline, "events"))
    if refund_required:
        refund = data_of(
            await broker.get("payment-agent", "get_refund_timeline", order_id=order_id), None
        )
        if refund is not None:
            refund["events"] = episode.filter(rows(refund, "events"), "event_at")
    broker.event(
        "handoff",
        "payment-agent",
        target="policy-agent",
        evidence_refs=broker.refs("payment", "refund"),
    )
    return {"timeline": timeline, "refund": refund, "refund_required": refund_required}


async def _shipment_agent(order_id: str, broker: EvidenceCollector, episode: Episode) -> dict:
    summary = data_of(
        await broker.get("shipment-agent", "get_shipment_summary", order_id=order_id), {}
    )
    broker.event(
        "handoff", "shipment-agent", target="policy-agent", evidence_refs=broker.refs("shipment")
    )
    return scope_shipment(summary, episode)


def _decide(
    case: dict,
    entity: dict,
    order: dict,
    payment: dict,
    shipment: dict,
    policy: dict,
    broker: EvidenceCollector,
    primary_override: str | None = None,
) -> dict[str, Any]:
    shipping, shipment_issues, shipment_conflicts = analyze_shipment(shipment)
    payments, payment_issues = analyze_payment(
        payment["timeline"],
        payment["refund"],
        order["expected"],
        refund_required=payment.get("refund_required", True),
        duplicate_unit=money(policy.get("rules", {}).get("duplicate_charge", {}).get("refund_brl")),
    )
    conflicts = [*order["conflicts"], *shipment_conflicts]
    authoritative = entity["order"]
    direct = entity["direct_order"]
    if authoritative and direct != authoritative:
        conflicts.append(
            {
                "field": "order_snapshot",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": entity["episode"].source,
                "resolution_code": "CASE_TIME_SCOPE",
            }
        )
    issues = list(payment_issues)
    if shipping["verdict"] != "conflicting":
        issues.extend(shipment_issues)
    if (payments["captured_total_brl"] or 0) > 0:
        status = authoritative.get("order_status")
        if status == "canceled":
            issues.append("canceled_order_paid")
        elif status == "unavailable":
            issues.append("unavailable_order_paid")
    issues = list(dict.fromkeys(issues))
    topics = [c.get("topic") for c in case.get("customer_request", {}).get("claims", [])]
    # A customer's topic can prioritize an independently established issue, never create one.
    priority = [topic for topic in topics if topic in issues]
    primary = next(iter(priority or issues), "insufficient_evidence")
    if primary_override in {*issues, "unsupported_claim", "insufficient_evidence"}:
        primary = primary_override
    if not issues and shipping["verdict"] == "on_time" and payments["verdict"] == "reconciled":
        primary = "unsupported_claim"
    rule = policy.get("rules", {}).get(primary, {})
    policy_ok = (
        policy.get("policy_version") == case.get("policy_version")
        and policy.get("currency") == "BRL"
        and isinstance(rule, dict)
        and bool(rule)
    )
    unresolved = any(c["selected_source"] is None for c in conflicts)
    uncertain = unresolved or not policy_ok or primary == "insufficient_evidence"
    case_status = (
        rule.get("case_status", "needs_investigation") if policy_ok else "needs_investigation"
    )
    confidence = 0.45 if uncertain else 0.98
    refund = Decimal(0)
    actions = [rule["recommended_action"]] if policy_ok else ["request_missing_evidence"]
    amount = money(rule.get("refund_brl")) if policy_ok else None
    refundable = money(payments["refundable_total_brl"])
    if amount and amount > 0:
        if refundable is None or amount > refundable or unresolved:
            uncertain = True
            actions = ["verify_refund_eligibility"]
        else:
            refund = amount
    if uncertain:
        case_status, confidence = "needs_investigation", min(confidence, 0.45)
        if unresolved:
            actions = ["resolve_source_conflict", "verify_refund_eligibility"]
        refund = Decimal(0)
    parties = rule.get("responsible_parties", []) if policy_ok and not uncertain else []
    # Generic policy examples do not authorize attributing a different seller's ID.
    actual_sellers = ids(order["items"], "seller_id")
    scoped_parties = []
    for party in parties:
        if party.get("party_type") == "seller":
            seller_ids = shipping["late_seller_ids"] or actual_sellers
            scoped_parties.extend(
                {"party_type": "seller", "party_id": seller} for seller in seller_ids
            )
        else:
            scoped_parties.append(party)
    refs = broker.refs()
    claims = []
    for claim in case.get("customer_request", {}).get("claims", []):
        topic = claim.get("topic")
        verdict = "insufficient_evidence"
        claim_refs = refs
        if topic in issues and not unresolved:
            verdict = "supported"
        elif topic == "requested_full_refund" and policy_ok and not uncertain:
            captured = money(payments["captured_total_brl"])
            verdict = (
                "supported"
                if captured and refund == captured
                else ("partially_supported" if refund else "unsupported")
            )
        elif primary in {"unsupported_claim", "valid_split_payment"} and not uncertain:
            verdict = "unsupported"
        if str(topic).startswith("late_delivery"):
            claim_refs = broker.refs("order", "item", "shipment", "policy")
        elif "payment" in str(topic) or topic == "duplicate_charge":
            claim_refs = broker.refs("payment", "item", "policy")
        elif "refund" in str(topic):
            claim_refs = broker.refs("order", "payment", "refund", "policy")
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": claim_refs,
            }
        )
    order_ids = entity["resolution"]["resolved_order_ids"]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": primary,
            "secondary_issues": [i for i in issues if i != primary],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": ids(order["items"], "order_item_id"),
            "seller_ids": actual_sellers,
            "payment_references": ids(rows(payment["timeline"], "events"), "payment_reference"),
            "shipment_ids": ids(rows(shipment, "events"), "shipment_id"),
        },
        "entity_resolution": entity["resolution"],
        "customer_context": entity["customer"],
        "shipment_analysis": shipping,
        "payment_analysis": payments,
        "root_cause_analysis": {
            "ranked_causes": [] if uncertain else [{"cause_code": primary.upper(), "rank": 1}],
            "responsible_parties": scoped_parties,
        },
        "claim_assessments": claims,
        "evidence_refs": refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": [
                {
                    "reason_code": primary.upper(),
                    "amount_brl": float(refund),
                    "entity_id": order_ids[0],
                }
            ]
            if refund
            else [],
        },
        "resolution_actions": actions,
    }


def verify_output(case: dict, output: dict, broker: EvidenceCollector) -> None:
    """Independent final gate over public schema, receipts and cross-field invariants."""
    broker.trace.contracts.validate_output(output, "verifier output")
    if output["case_id"] != case["case_id"]:
        raise ValueError("Verifier: case mismatch")
    resolved = set(output["entity_resolution"]["resolved_order_ids"])
    if resolved != set(output["affected_entities"]["order_ids"]):
        raise ValueError("Verifier: affected orders do not match resolved orders")
    if resolved & set(output["entity_resolution"]["rejected_candidates"]):
        raise ValueError("Verifier: rejected candidate was selected")
    refs = set(output["evidence_refs"])
    if not refs:
        raise ValueError("Verifier: no MCP evidence receipts for case")
    if not refs <= broker.receipts.keys():
        raise ValueError("Verifier: evidence was not collected for this case")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= refs:
            raise ValueError("Verifier: unlinked claim evidence")
    finance = output["financial_resolution"]
    recommended = money(finance["recommended_refund_brl"])
    line_total = sum((money(line["amount_brl"]) for line in finance["refund_lines"]), Decimal(0))
    if line_total != recommended:
        raise ValueError("Verifier: refund lines do not reconcile")
    payments = output["payment_analysis"]
    captured = money(payments["captured_total_brl"])
    refunded = money(payments["refunded_total_brl"])
    refundable = money(payments["refundable_total_brl"])
    if refundable is not None and (
        captured is None or refunded is None or captured - refunded != refundable
    ):
        raise ValueError("Verifier: invalid refundable balance")
    if recommended and (refundable is None or recommended > refundable):
        raise ValueError("Verifier: refund exceeds verified balance")
    if output["assessment"]["case_status"] != "action_required" and recommended:
        raise ValueError("Verifier: refund requires action_required status")
    for entry in output["data_conflicts"]:
        if entry["selected_source"] and entry["selected_source"] not in entry["sources"]:
            raise ValueError("Verifier: selected source is outside conflict sources")
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if (
            party["party_type"] == "seller"
            and party["party_id"] not in output["affected_entities"]["seller_ids"]
        ):
            raise ValueError("Verifier: responsible seller is outside the resolved order")
    broker.event(
        "verification_completed",
        "verifier",
        target="coordinator",
        decision_code="VALIDATED",
        attributes={"evidence_count": len(refs), "mcp_calls": broker.calls},
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run a finite A2A graph; public schemas are never extended or bypassed."""
    broker = EvidenceCollector(case["case_id"], gateway, trace)
    entity = await _resolve(case, broker)
    order = {"items": [], "expected": None, "conflicts": []}
    payment = {"timeline": {}, "refund": None, "refund_required": True}
    shipment = {}
    if entity["resolution"]["status"] == "resolved":
        order_id = entity["resolution"]["resolved_order_ids"][0]
        for actor in ("order-agent", "payment-agent", "shipment-agent"):
            broker.event(
                "task_assigned", "coordinator", target=actor, attributes={"order_id": order_id}
            )
        order, payment, shipment = await asyncio.gather(
            _order_agent(case, order_id, broker, entity["episode"]),
            _payment_agent(case, order_id, broker, entity["episode"]),
            _shipment_agent(order_id, broker, entity["episode"]),
        )
    broker.event("task_assigned", "coordinator", target="policy-agent")
    policy = data_of(
        await broker.get("policy-agent", "get_policy", policy_version=case["policy_version"]), {}
    )
    baseline = _decide(case, entity, order, payment, shipment, policy, broker)
    allowed_issues = sorted(
        set(baseline["assessment"]["secondary_issues"])
        | {baseline["assessment"]["primary_issue"]}
    )
    model_choice = baseline["assessment"]["primary_issue"]
    if len(allowed_issues) > 1:
        broker.event("task_assigned", "coordinator", target="semantic-agent")
        model_choice = await select_primary_issue(case, baseline, allowed_issues)
        broker.event(
            "handoff",
            "semantic-agent",
            target="coordinator",
            decision_code=model_choice.upper(),
        )
    output = _decide(
        case,
        entity,
        order,
        payment,
        shipment,
        policy,
        broker,
        primary_override=model_choice,
    )
    broker.event(
        "policy_decided",
        "policy-agent",
        target="verifier",
        decision_code=output["assessment"]["primary_issue"].upper(),
        evidence_refs=broker.refs("policy"),
    )
    broker.event("handoff", "policy-agent", target="verifier")
    verify_output(case, output, broker)
    evidence_dir = trace.path.parent / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / f"{case['case_id']}.json").write_text(
        json.dumps(
            {"case_id": case["case_id"], "receipts": list(broker.receipts.values())},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return output
