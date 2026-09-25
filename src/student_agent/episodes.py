"""Resolve repeated order snapshots by case time, independently of customer claims."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .analysis import money, rows, timestamp


@dataclass(frozen=True)
class Episode:
    order: dict[str, Any]
    start: datetime | None
    end: datetime | None
    source: str
    scoped: bool = True

    def contains(self, value: Any) -> bool:
        time = timestamp(value)
        if self.start is None or time is None:
            return True
        return time >= self.start and (self.end is None or time < self.end)

    def filter(self, values: list[dict], time_field: str) -> list[dict]:
        return [deepcopy(value) for value in values if self.contains(value.get(time_field))]


def select_episode(case: dict, order: dict, history: dict) -> Episode:
    opened = timestamp(case.get("opened_at"))
    snapshots = [
        row for row in rows(history, "orders") if row.get("order_id") == order.get("order_id")
    ]
    if order and order not in snapshots:
        snapshots.append(order)
    dated = [row for row in snapshots if timestamp(row.get("order_purchase_timestamp"))]
    if not opened or not dated:
        return Episode(deepcopy(order), None, None, "get_order")
    eligible = [row for row in dated if timestamp(row["order_purchase_timestamp"]) <= opened]
    if not eligible:
        return Episode({}, None, None, "get_customer_history", scoped=False)
    start = max(timestamp(row["order_purchase_timestamp"]) for row in eligible)
    selected = [row for row in eligible if timestamp(row["order_purchase_timestamp"]) == start]
    # The direct authoritative row can break ties only within the same purchase episode.
    if order in selected:
        chosen, source = order, "get_order"
    elif all(row == selected[0] for row in selected):
        chosen, source = selected[0], "get_customer_history"
    else:
        return Episode({}, start, None, "get_customer_history", scoped=False)
    later = [
        timestamp(row["order_purchase_timestamp"])
        for row in dated
        if timestamp(row["order_purchase_timestamp"]) > start
    ]
    return Episode(deepcopy(chosen), start, min(later) if later else None, source)


def scope_payment(timeline: dict, episode: Episode) -> dict:
    result = deepcopy(timeline)
    events = episode.filter(rows(timeline, "events"), "event_at")
    result["events"] = events
    capture_values = {
        money(e.get("amount_brl")) for e in events if e.get("event_type") == "captured"
    }
    payments = []
    for row in rows(timeline, "payments"):
        if capture_values and money(row.get("payment_value")) not in capture_values:
            continue
        if row not in payments:
            payments.append(deepcopy(row))
    result["payments"] = payments
    return result


def scope_shipment(summary: dict, episode: Episode) -> dict:
    result = deepcopy(summary)
    result["events"] = episode.filter(rows(summary, "events"), "event_at")
    result["shipping_limits"] = episode.filter(
        rows(summary, "shipping_limits"), "shipping_limit_at"
    )
    if episode.start is not None:
        fields = {
            "order_status": "order_status",
            "delivered_carrier_at": "order_delivered_carrier_date",
            "delivered_customer_at": "order_delivered_customer_date",
            "estimated_delivery_at": "order_estimated_delivery_date",
        }
        for target, source in fields.items():
            if source in episode.order:
                result[target] = episode.order[source]
    return result
