from decimal import Decimal

from student_agent.analysis import analyze_items, analyze_payment
from student_agent.episodes import scope_payment, scope_shipment, select_episode


def snapshots():
    current = {
        "order_id": "order-a",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-05-11T09:00:00-03:00",
    }
    history = {
        "orders": [
            current,
            {
                "order_id": "order-a",
                "order_status": "canceled",
                "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
                "order_delivered_customer_date": None,
            },
        ]
    }
    return current, history


def test_case_date_resolves_historical_order_without_using_claim():
    direct, history = snapshots()
    case = {
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {"claims": [{"topic": "unavailable_order_paid"}]},
    }
    episode = select_episode(case, direct, history)
    assert episode.order["order_status"] == "canceled"
    assert episode.source == "get_customer_history"
    assert episode.contains("2017-12-21T10:00:00-03:00")
    assert not episode.contains("2018-05-11T10:00:00-03:00")


def test_future_only_order_is_not_resolved():
    direct, history = snapshots()
    assert not select_episode({"opened_at": "2017-01-01T00:00:00Z"}, direct, history).scoped


def test_capture_and_shipment_are_scoped_to_selected_purchase():
    direct, history = snapshots()
    episode = select_episode({"opened_at": "2018-01-01T09:00:00-03:00"}, direct, history)
    timeline = {
        "payments": [{"payment_value": "79.00"}, {"payment_value": "89.00"}],
        "events": [
            {
                "event_at": "2017-12-20T10:00:00-03:00",
                "event_type": "captured",
                "amount_brl": "79.00",
                "status": "confirmed",
            },
            {
                "event_at": "2018-05-11T10:00:00-03:00",
                "event_type": "captured",
                "amount_brl": "89.00",
                "status": "confirmed",
            },
        ],
    }
    scoped = scope_payment(timeline, episode)
    assert len(scoped["events"]) == 1
    assert scoped["payments"] == [{"payment_value": "79.00"}]
    payment, _ = analyze_payment(scoped, None, Decimal(89), refund_required=False)
    assert payment["captured_total_brl"] == 79
    assert payment["verdict"] == "reconciled"
    shipment = scope_shipment(
        {"order_status": "delivered", "delivered_customer_at": "2018-05-20T00:00:00Z"}, episode
    )
    assert shipment["order_status"] == "canceled"
    assert shipment["delivered_customer_at"] is None


def test_shipping_date_changes_do_not_invalidate_stable_item_money():
    first = {
        "order_item_id": "item-a",
        "price": "79.00",
        "freight_value": "10.00",
        "shipping_limit_date": "2018-01-01",
    }
    second = {**first, "shipping_limit_date": "2018-05-01"}
    total, conflicts = analyze_items([first, second])
    assert total == Decimal(89)
    assert conflicts == []


def test_equal_split_is_not_duplicate_but_policy_correlated_overcapture_is():
    def timeline(amount):
        return {
            "payments": [
                {"payment_sequential": "1", "payment_value": amount},
                {"payment_sequential": "2", "payment_value": amount},
            ],
            "events": [{"event_type": "captured", "status": "confirmed", "amount_brl": amount}] * 2,
        }

    _, issues = analyze_payment(
        timeline("44.5"), None, Decimal(89), refund_required=False, duplicate_unit=Decimal(64)
    )
    assert issues == ["valid_split_payment"]
    payment, issues = analyze_payment(
        timeline("64"), None, Decimal(89), refund_required=False, duplicate_unit=Decimal(64)
    )
    assert issues == ["duplicate_charge"]
    assert payment["verdict"] == "duplicate_capture"
    assert payment["captured_total_brl"] == 128


def test_refund_lifecycle_does_not_double_count_same_refund():
    timeline = {"events": [{"event_type": "captured", "status": "confirmed", "amount_brl": 89}]}
    refund = {
        "events": [
            {
                "refund_id": "r-a",
                "status": "confirmed",
                "amount_brl": 40,
                "event_at": "2018-01-01T00:00:00Z",
            },
            {
                "refund_id": "r-a",
                "status": "completed",
                "amount_brl": 40,
                "event_at": "2018-01-02T00:00:00Z",
            },
        ]
    }
    payment, _ = analyze_payment(timeline, refund, Decimal(89))
    assert payment["refunded_total_brl"] == 40
    assert payment["refundable_total_brl"] == 49
