"""Deterministic specialists operating exclusively on scoped MCP evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result.quantize(Decimal("0.01")) if result.is_finite() and result >= 0 else None


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def ids(rows: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(row[key]) for row in rows if row.get(key) is not None})


def rows(value: Any, key: str | None = None) -> list[dict[str, Any]]:
    if key and isinstance(value, dict):
        value = value.get(key, [])
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        return []
    return value


def conflict(field: str, sources: list[str], selected: str | None = None) -> dict[str, Any]:
    return {
        "field": field,
        "sources": sources,
        "selected_source": selected,
        "resolution_code": "AUTHORITATIVE_SOURCE" if selected else "UNRESOLVED",
    }


def analyze_items(items: list[dict[str, Any]]) -> tuple[Decimal | None, list[dict[str, Any]]]:
    unique: dict[str, dict[str, Any]] = {}
    conflicts = []
    for item in items:
        item_id = str(item.get("order_item_id", ""))
        if not item_id:
            return None, []
        previous = unique.get(item_id)
        financial_fields = ("price", "freight_value", "seller_id", "product_id")
        if previous and any(previous.get(key) != item.get(key) for key in financial_fields):
            conflicts.append(conflict("items." + item_id, ["item_snapshot_1", "item_snapshot_2"]))
        unique[item_id] = item
    amounts = [
        (money(item.get("price")), money(item.get("freight_value"))) for item in unique.values()
    ]
    if not amounts or conflicts or any(a is None or b is None for a, b in amounts):
        return None, conflicts
    return sum((a + b for a, b in amounts), Decimal(0)), []


def analyze_shipment(summary: dict[str, Any]) -> tuple[dict[str, Any], list[str], list]:
    result = {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False}
    if not summary:
        return result, [], []
    carrier = timestamp(summary.get("delivered_carrier_at"))
    delivered = timestamp(summary.get("delivered_customer_at"))
    estimated = timestamp(summary.get("estimated_delivery_at"))
    limits = rows(summary, "shipping_limits")
    valid_limits = all(timestamp(row.get("shipping_limit_at")) for row in limits)
    complete = bool(carrier and delivered and estimated and limits and valid_limits)
    temporal_conflict = bool(carrier and delivered and carrier > delivered)
    late_sellers = ids(
        [
            row
            for row in limits
            if carrier
            and timestamp(row.get("shipping_limit_at"))
            and carrier > timestamp(row["shipping_limit_at"])
        ],
        "seller_id",
    )
    events = [
        event
        for event in rows(summary, "events")
        if event.get("status") in {"confirmed", "completed", "delivered"}
    ]
    event_issues = set()
    for event in events:
        if event.get("event_type") == "delivered_late":
            if event.get("actor") == "seller":
                event_issues.add("late_delivery_seller")
            elif event.get("actor") == "logistics_provider":
                event_issues.add("late_delivery_logistics")
    verdict = "insufficient_evidence"
    issues: list[str] = []
    if delivered and estimated:
        if delivered <= estimated:
            verdict = "on_time"
        elif late_sellers:
            verdict, issues = "seller_delay", ["late_delivery_seller"]
        elif carrier and limits and valid_limits:
            verdict, issues = "logistics_delay", ["late_delivery_logistics"]
    if len(event_issues) == 1:
        event_issue = next(iter(event_issues))
        if verdict == "insufficient_evidence":
            verdict = "seller_delay" if event_issue.endswith("seller") else "logistics_delay"
            issues = [event_issue]
        elif event_issue not in issues:
            temporal_conflict = True
    if len(event_issues) > 1:
        temporal_conflict = True
    conflicts = []
    if temporal_conflict:
        conflicts.append(conflict("shipment.delivery", ["shipment_summary", "shipment_events"]))
        verdict = "conflicting"
        issues = sorted(set(issues) | event_issues)
    result.update(
        verdict=verdict,
        late_seller_ids=late_sellers,
        timeline_complete=complete and not temporal_conflict,
    )
    return result, issues, conflicts


def analyze_payment(
    timeline: dict[str, Any],
    refund: dict[str, Any] | None,
    expected: Decimal | None,
    *,
    refund_required: bool = True,
    duplicate_unit: Decimal | None = None,
) -> tuple[dict[str, Any], list[str]]:
    result = {
        "verdict": "insufficient_evidence",
        "captured_total_brl": None,
        "refunded_total_brl": None,
        "refundable_total_brl": None,
    }
    events = rows(timeline, "events")
    captures = [
        e
        for e in events
        if e.get("event_type") == "captured"
        and e.get("status") in {"confirmed", "completed", "succeeded"}
    ]
    values = [money(e.get("amount_brl")) for e in captures]
    if not captures or any(value is None for value in values):
        return result, []
    captured = sum(values, Decimal(0))
    result["captured_total_brl"] = float(captured)
    # Missing refund evidence is unknown, never silently treated as zero refunded.
    refund_events = rows(refund, "events") if refund is not None else []
    # A refund lifecycle updates one balance; do not sum its requested/settled snapshots.
    latest: dict[str, dict[str, Any]] = {}
    for event in sorted(
        refund_events,
        key=lambda e: timestamp(e.get("event_at")) or datetime.min.replace(tzinfo=UTC),
    ):
        key = str(event.get("refund_id") or event.get("refund_reference") or "order")
        latest[key] = event
    successful = [
        e
        for e in latest.values()
        if e.get("status") in {"completed", "confirmed", "succeeded", "refunded"}
    ]
    refund_values = [money(e.get("amount_brl")) for e in successful]
    if (refund is not None or not refund_required) and all(v is not None for v in refund_values):
        refunded = sum(refund_values, Decimal(0))
        if refunded <= captured:
            result["refunded_total_brl"] = float(refunded)
            result["refundable_total_brl"] = float(captured - refunded)
        else:
            return result, []
    issues = []
    statuses = {e.get("status") for e in latest.values()}
    if "failed" in statuses:
        issues.append("refund_failed")
    elif "pending" in statuses or "requested" in statuses:
        issues.append("refund_pending")
    duplicate = any(
        e.get("event_type") in {"duplicate_capture", "duplicate_charge"}
        and e.get("status") not in {"rejected", "failed"}
        for e in events
    )
    transactions = [e.get("transaction_id") or e.get("payment_reference") for e in captures]
    known = [ref for ref in transactions if ref]
    duplicate = duplicate or len(set(known)) != len(known)
    # Equal amounts alone do not prove duplication: also require overcapture and a
    # duplicate-refund unit corroborated by the public policy for this case.
    duplicate = duplicate or bool(
        len(values) > 1
        and len(set(values)) == 1
        and expected is not None
        and captured > expected
        and duplicate_unit is not None
        and values[0] == duplicate_unit
    )
    mismatch = any(
        e.get("event_type") == "reconciliation_mismatch" and e.get("status") == "open"
        for e in events
    )
    if duplicate:
        issues.append("duplicate_charge")
    elif mismatch:
        issues.append("payment_mismatch")
    if issues:
        mapping = {"duplicate_charge": "duplicate_capture", "payment_mismatch": "capture_mismatch"}
        result["verdict"] = mapping.get(issues[0], issues[0])
    elif result["refunded_total_brl"] and result["refunded_total_brl"] == float(captured):
        result["verdict"] = "refunded"
    else:
        base_amounts = [money(row.get("payment_value")) for row in rows(timeline, "payments")]
        ledger_total = (
            sum(base_amounts, Decimal(0))
            if base_amounts and all(value is not None for value in base_amounts)
            else expected
        )
        reconciled = ledger_total is not None and abs(captured - ledger_total) <= Decimal("0.01")
        if not reconciled:
            result["verdict"] = (
                "capture_mismatch" if ledger_total is not None else "insufficient_evidence"
            )
            return result, ["payment_mismatch"] if ledger_total is not None else []
        result["verdict"] = "reconciled"
        payments = rows(timeline, "payments")
        if len(ids(payments, "payment_sequential")) > 1 and expected == captured:
            issues.append("valid_split_payment")
    return result, issues
