import json
from decimal import Decimal

import httpx
import pytest
import respx

from ibkr_agent.adapters.cpapi import CpapiBroker, CpapiClient, CpapiError
from trading_core.domain.models import (
    BracketRequest,
    OrderRequest,
    OrderSide,
    OrderType,
    TrailingType,
)

BASE = "https://localhost:5000/v1/api"
ACCT = "DU123"


async def _resolver(symbol: str) -> int:
    return 265598


def _sent_order(route) -> dict:
    body = json.loads(route.calls.last.request.content)
    return body["orders"][0]


@respx.mock
async def test_cashqty_order_with_reply_loop():
    warning = {"id": "q1", "message": ["...without market data..."], "messageIds": ["o354"]}
    orders = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(200, json=[warning])
    )
    respx.post(f"{BASE}/iserver/reply/q1").mock(
        return_value=httpx.Response(
            200, json=[{"order_id": "123", "order_status": "Submitted", "encrypt_message": "1"}]
        )
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver, id_factory=lambda: "fixed-id")

    result = await broker.place_order(
        OrderRequest(symbol="aapl", side=OrderSide.BUY, cash_qty=Decimal("50"))
    )

    assert result.order_id == "123"
    assert result.status.value == "submitted"
    order = _sent_order(orders)
    assert order["cashQty"] == 50
    assert order["conid"] == 265598
    assert order["cOID"] == "fixed-id"
    assert "quantity" not in order
    await client.aclose()


@respx.mock
async def test_unknown_warning_blocks_and_declines_order():
    respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(
            200, json=[{"id": "q1", "message": ["High risk!"], "messageIds": ["o999"]}]
        )
    )
    decline = respx.post(f"{BASE}/iserver/reply/q1").mock(
        return_value=httpx.Response(200, json=[{"order_id": "0", "order_status": "Cancelled"}])
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    with pytest.raises(CpapiError, match="blocked"):
        await broker.place_order(
            OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("50"))
        )

    # The pending order was declined (confirmed:false) so it isn't left Inactive.
    assert decline.called
    assert json.loads(decline.calls.last.request.content) == {"confirmed": False}
    await client.aclose()


@respx.mock
async def test_quantity_order_body():
    orders = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(200, json=[{"order_id": "9", "order_status": "Submitted"}])
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=3)
    )

    assert result.order_id == "9"
    order = _sent_order(orders)
    assert order["quantity"] == 3
    assert "cashQty" not in order
    await client.aclose()


@respx.mock
async def test_cancel_order_fills_symbol_from_live_orders():
    order = {"orderId": 55, "ticker": "AAPL", "side": "BUY", "status": "Submitted"}
    respx.get(f"{BASE}/iserver/account/orders").mock(
        return_value=httpx.Response(200, json={"orders": [order]})
    )
    respx.delete(f"{BASE}/iserver/account/{ACCT}/order/55").mock(
        return_value=httpx.Response(200, json={"msg": "ok", "order_id": "55"})
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.cancel_order("55")

    assert result.symbol == "AAPL"
    assert result.side.value == "BUY"
    # A bare ack ("msg" only, no order_status) means the cancel was REQUESTED, not
    # confirmed — we report pending and let the caller confirm, never a false cancel.
    assert result.status.value == "pending"
    await client.aclose()


@respx.mock
async def test_cancel_order_reports_cancelled_only_when_gateway_confirms():
    respx.get(f"{BASE}/iserver/account/orders").mock(
        return_value=httpx.Response(200, json={"orders": []})
    )
    respx.delete(f"{BASE}/iserver/account/{ACCT}/order/77").mock(
        return_value=httpx.Response(200, json={"order_id": "77", "order_status": "Cancelled"})
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.cancel_order("77")

    assert result.status.value == "cancelled"
    await client.aclose()


@respx.mock
async def test_order_status_maps_inactive():
    # CPAPI signals a rejected/dead order as "Inactive" (not "Rejected") — must not be UNKNOWN.
    respx.get(f"{BASE}/iserver/account/order/status/9").mock(
        return_value=httpx.Response(
            200, json={"order_id": 9, "order_status": "Inactive", "symbol": "AAPL", "side": "B"}
        )
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)
    result = await broker.get_order_status("9")
    assert result.status.value == "inactive"
    await client.aclose()


@respx.mock
async def test_cancel_order_question_returns_pending_not_raise():
    # A cancel that comes back as a confirmation question must NOT be declined+raised
    # through the order allow-list — report pending with the text and let the caller decide.
    respx.get(f"{BASE}/iserver/account/orders").mock(
        return_value=httpx.Response(200, json={"orders": []})
    )
    respx.delete(f"{BASE}/iserver/account/{ACCT}/order/99").mock(
        return_value=httpx.Response(
            200, json=[{"id": "q1", "message": ["Confirm cancel?"], "messageIds": ["o999"]}]
        )
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.cancel_order("99")  # must not raise

    assert result.status.value == "pending"
    assert "Confirm cancel" in (result.message or "")
    await client.aclose()


@respx.mock
async def test_cancel_order_parses_list_shaped_ack():
    # A list-shaped confirmation must be read, not dropped to "pending".
    respx.get(f"{BASE}/iserver/account/orders").mock(
        return_value=httpx.Response(200, json={"orders": []})
    )
    respx.delete(f"{BASE}/iserver/account/{ACCT}/order/88").mock(
        return_value=httpx.Response(200, json=[{"order_id": "88", "order_status": "Cancelled"}])
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.cancel_order("88")

    assert result.status.value == "cancelled"
    await client.aclose()


@respx.mock
async def test_limit_order_body_carries_price():
    orders = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(200, json=[{"order_id": "7", "order_status": "Submitted"}])
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    await broker.place_order(
        OrderRequest(
            symbol="AAPL", side=OrderSide.BUY, quantity=2,
            order_type=OrderType.LIMIT, limit_price=Decimal("180.5"),
        )
    )

    order = _sent_order(orders)
    assert order["orderType"] == "LMT"
    assert order["price"] == 180.5
    assert order["quantity"] == 2
    await client.aclose()


@respx.mock
async def test_stop_order_body_carries_stop_in_price():
    # CPAPI carries a plain STOP's trigger in `price`, not `auxPrice` (validated live).
    orders = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(200, json=[{"order_id": "8", "order_status": "Submitted"}])
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    await broker.place_order(
        OrderRequest(
            symbol="AAPL", side=OrderSide.SELL, quantity=1,
            order_type=OrderType.STOP, stop_price=Decimal("170"),
        )
    )

    order = _sent_order(orders)
    assert order["orderType"] == "STP"
    assert order["price"] == 170
    assert "auxPrice" not in order
    await client.aclose()


@respx.mock
async def test_stop_limit_order_body_carries_both_prices():
    orders = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(200, json=[{"order_id": "8", "order_status": "Submitted"}])
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    await broker.place_order(
        OrderRequest(
            symbol="AAPL", side=OrderSide.SELL, quantity=1, order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("170"), limit_price=Decimal("169"),
        )
    )

    order = _sent_order(orders)
    assert order["orderType"] == "STOP_LIMIT"
    assert order["price"] == 169  # limit
    assert order["auxPrice"] == 170  # stop trigger
    await client.aclose()


@respx.mock
async def test_bracket_submits_three_linked_orders():
    sent = {}

    def capture(request):
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json=[
            {"order_id": "100", "local_order_id": "parent", "order_status": "Submitted"},
            {"order_id": "101", "local_order_id": "tp", "order_status": "Submitted"},
            {"order_id": "102", "local_order_id": "sl", "order_status": "Submitted"},
        ])

    respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(side_effect=capture)
    client = CpapiClient(BASE)
    coids = iter(["parent", "tp", "sl"])
    broker = CpapiBroker(client, ACCT, _resolver, id_factory=lambda: next(coids))

    results = await broker.place_bracket(
        BracketRequest(
            entry=OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=2),
            take_profit_price=Decimal("200"), stop_loss_price=Decimal("150"),
        )
    )

    legs = sent["body"]["orders"]
    assert [o["orderType"] for o in legs] == ["MKT", "LMT", "STP"]
    assert legs[1]["side"] == "SELL" and legs[2]["side"] == "SELL"
    assert legs[1]["parentId"] == "parent" and legs[2]["parentId"] == "parent"
    assert legs[1]["price"] == 200  # take-profit limit
    assert legs[2]["price"] == 150  # stop-loss trigger (STP carries it in price)
    assert {r.message for r in results} == {"entry", "take_profit", "stop_loss"}
    assert results[0].order_id == "100"
    await client.aclose()


@respx.mock
async def test_trailing_stop_body_carries_trailing_fields():
    orders = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(200, json=[{"order_id": "T", "order_status": "Submitted"}])
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    await broker.place_order(
        OrderRequest(
            symbol="AAPL", side=OrderSide.SELL, quantity=2, order_type=OrderType.TRAIL,
            trailing_amount=Decimal("3"), trailing_type=TrailingType.AMOUNT,
        )
    )

    order = _sent_order(orders)
    assert order["orderType"] == "TRAIL"
    assert order["trailingAmt"] == 3
    assert order["trailingType"] == "amt"
    await client.aclose()


@respx.mock
async def test_order_post_503_retried_when_order_did_not_land():
    posts = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        side_effect=[
            httpx.Response(503, json={"error": "busy"}),
            httpx.Response(200, json=[{"order_id": "9", "order_status": "Submitted"}]),
        ]
    )
    respx.get(f"{BASE}/iserver/account/orders").mock(
        return_value=httpx.Response(200, json={"orders": []})  # nothing landed
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver, id_factory=lambda: "coid-1")

    result = await broker.place_order(OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=1))

    assert result.order_id == "9"
    assert posts.call_count == 2  # retried after confirming it hadn't landed
    await client.aclose()


@respx.mock
async def test_order_post_503_not_resent_when_landing_is_unknown():
    # 503 on the POST, and the follow-up lookup ALSO fails → we cannot tell whether the
    # order landed, so we must NOT resend (would risk a duplicate). Expect a raise, and
    # exactly one POST attempt.
    posts = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        side_effect=[httpx.Response(503, json={"error": "busy"})]
    )
    respx.get(f"{BASE}/iserver/account/orders").mock(
        return_value=httpx.Response(503, json={"error": "busy"})  # lookup also fails
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver, id_factory=lambda: "coid-1")

    with pytest.raises(CpapiError, match="UNKNOWN whether the order landed"):
        await broker.place_order(OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=1))
    assert posts.call_count == 1  # never resent on an inconclusive lookup
    await client.aclose()


@respx.mock
async def test_order_post_503_not_resent_when_already_landed():
    # The 503 came after the order landed — we must NOT resend (would double-submit).
    posts = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        side_effect=[httpx.Response(503, json={"error": "busy"})]
    )
    respx.get(f"{BASE}/iserver/account/orders").mock(
        return_value=httpx.Response(200, json={"orders": [
            {"orderId": 55, "order_ref": "coid-1", "status": "Submitted",
             "ticker": "AAPL", "side": "BUY"},
        ]})
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver, id_factory=lambda: "coid-1")

    result = await broker.place_order(OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=1))

    assert result.order_id == "55"
    assert posts.call_count == 1  # found by cOID, not re-sent
    await client.aclose()


@respx.mock
async def test_get_order_status_parses_fill():
    # Field shape captured live from /iserver/account/order/status/{id}:
    # `symbol` (not ticker), `side` as "B"/"S", `cum_fill`, `order_status`.
    # A SHARE-sized order: no cash fields, so `cum_fill` is already in shares.
    respx.get(f"{BASE}/iserver/account/order/status/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": 123, "order_status": "Filled", "symbol": "AAPL",
                "contract_description_1": "AAPL", "side": "B", "size": "0.0066",
                "cum_fill": "0.0066", "average_price": "298.96", "order_type": "LIMIT",
            },
        )
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.get_order_status("123")

    assert result.order_id == "123"
    assert result.status.value == "filled"
    assert result.symbol == "AAPL"
    assert result.side is OrderSide.BUY
    assert result.filled_quantity == Decimal("0.0066")
    assert result.avg_price == Decimal("298.96")
    assert result.is_cash_quantity is False
    assert result.filled_cash is None  # a share order reports no cash size
    assert result.quantity_is_estimate is False
    await client.aclose()


# The EXACT payload IBKR returned for a live US$2 cash-quantity BUY of AAPL that filled
# 0.0063 shares @ 317.25 (order_status path). Note `cum_fill` = 2.0 = DOLLARS, while the
# real share count hides in `total_size`. This fixture is the bug: any mapper that reads
# `cum_fill` as shares reports 2 shares for a 0.0063-share position (317x).
_LIVE_CASH_FILL_STATUS = {
    "order_id": 456, "order_status": "Filled", "symbol": "AAPL", "side": "B",
    "size": "0.0", "total_size": "0.0063",
    "cash_quantity": "0.0", "total_cash_quantity": "2.0",
    "cum_fill": "2.0", "size_and_fills": "2 USD", "average_price": "317.25",
    "order_description": "Bought 0.0063 AAPL Market, Day", "order_type": "MARKET",
}


@respx.mock
async def test_cash_order_status_reports_shares_not_dollars():
    # THE REAL-MONEY BUG: IBKR reports a cashQty order's cum_fill in DOLLARS, in the same
    # field that carries SHARES on a share-sized order. filled_quantity must be SHARES —
    # a caller sizing a stop from 2.0 against a 0.0063-share position would go naked short.
    respx.get(f"{BASE}/iserver/account/order/status/456").mock(
        return_value=httpx.Response(200, json=_LIVE_CASH_FILL_STATUS)
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.get_order_status("456")

    assert result.filled_quantity == Decimal("0.0063")  # SHARES (total_size), never 2.0
    assert result.filled_cash == Decimal("2.0")  # the US$ actually spent
    assert result.is_cash_quantity is True
    assert result.quantity_is_estimate is False  # fully filled → total_size is exact
    assert result.avg_price == Decimal("317.25")
    await client.aclose()


@respx.mock
async def test_partial_cash_fill_flags_an_estimated_share_count():
    # A PARTIAL cash fill: `total_size` is the ORDER's size, not what executed, so the exact
    # executed share count is NOT in the payload. We derive it from filled_cash / avg_price
    # and flag it — never present a derived number as exact.
    respx.get(f"{BASE}/iserver/account/order/status/457").mock(
        return_value=httpx.Response(
            200,
            json={
                **_LIVE_CASH_FILL_STATUS, "order_id": 457, "order_status": "Submitted",
                "cum_fill": "1.0", "cash_quantity": "1.0", "size": "0.0031",
            },
        )
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.get_order_status("457")

    # 1.0 USD / 317.25 = 0.003152... → floored to IBKR's 4-dp share precision (never over-state).
    assert result.filled_quantity == Decimal("0.0031")
    assert result.quantity_is_estimate is True
    assert result.filled_cash == Decimal("1.0")
    assert "ESTIMATE" in (result.message or "")
    await client.aclose()


@respx.mock
async def test_partial_cash_fill_without_price_reports_unknown_shares():
    # No average price → the share count is NOT derivable. Report None (and the cash),
    # never a number a caller could mistake for a share count.
    respx.get(f"{BASE}/iserver/account/order/status/458").mock(
        return_value=httpx.Response(
            200,
            json={
                **_LIVE_CASH_FILL_STATUS, "order_id": 458, "order_status": "Submitted",
                "cum_fill": "1.0", "average_price": "",
            },
        )
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.get_order_status("458")

    assert result.filled_quantity is None
    assert result.filled_cash == Decimal("1.0")
    assert result.quantity_is_estimate is True
    assert "UNKNOWN" in (result.message or "")
    await client.aclose()


@respx.mock
async def test_open_orders_cash_order_reports_shares_and_avg_price():
    # The EXACT payload from /iserver/account/orders for the same live US$2 fill.
    # Two bugs at once: `filledQuantity` is DOLLARS (must map to shares), and `avgPrice`
    # was being dropped entirely (avg_price came back null though IBKR sent "317.25").
    respx.get(f"{BASE}/iserver/account/orders").mock(
        return_value=httpx.Response(200, json={"orders": [{
            "orderId": 456, "ticker": "AAPL", "side": "BUY", "status": "Filled",
            "cashQty": 0.0, "cashCcy": "USD", "sizeAndFills": "2 USD",
            "orderDesc": "Bought 0.0063 AAPL Market, Day",
            "remainingQuantity": 0.0, "filledQuantity": 2.0,
            "totalSize": 0.0063, "totalCashSize": 2.0, "avgPrice": "317.25",
        }]})
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    orders = await broker.get_live_orders()

    assert orders[0].filled_quantity == Decimal("0.0063")  # SHARES, not 2.0 dollars
    assert orders[0].filled_cash == Decimal("2.0")
    assert orders[0].is_cash_quantity is True
    assert orders[0].avg_price == Decimal("317.25")  # was silently dropped
    await client.aclose()


@respx.mock
async def test_open_orders_share_order_passes_quantity_through():
    respx.get(f"{BASE}/iserver/account/orders").mock(
        return_value=httpx.Response(200, json={"orders": [{
            "orderId": 60, "ticker": "AAPL", "side": "BUY", "status": "Submitted",
            "filledQuantity": 3.0, "remainingQuantity": 2.0, "totalSize": 5.0,
            "avgPrice": "180.10",
        }]})
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    orders = await broker.get_live_orders()

    assert orders[0].filled_quantity == Decimal("3.0")  # already shares — untouched
    assert orders[0].filled_cash is None
    assert orders[0].is_cash_quantity is False
    assert orders[0].avg_price == Decimal("180.10")
    await client.aclose()


@respx.mock
async def test_cash_buy_ack_is_flagged_as_a_cash_quantity_order():
    respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(200, json=[{"order_id": "5", "order_status": "Submitted"}])
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    cash = await broker.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("2"))
    )
    shares = await broker.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=Decimal("1"))
    )

    assert cash.is_cash_quantity is True
    assert shares.is_cash_quantity is False
    await client.aclose()


@respx.mock
async def test_preview_order_parses_whatif():
    # Shape captured from a live retail whatif: unit-suffixed money strings,
    # warnings as "<code>/<html>", null margin blocks, funds impact in `data`.
    respx.post(f"{BASE}/iserver/account/{ACCT}/orders/whatif").mock(
        return_value=httpx.Response(
            200,
            json={
                "amount": {
                    "amount": "2 USD (0.0067 Shares)",
                    "commission": "0.02 USD",
                    "total": "2.02 USD",
                },
                "equity": None,
                "initial": None,
                "warn": "22/<h4>Market Order Confirmation</h4>&nbsp;A Market Order...",
                "warns": [
                    "22/<h4>Market Order Confirmation</h4>&nbsp;A Market Order...",
                    "29/<html><h4>Cash Quantity Order Confirmation</h4> Orders...</html>",
                ],
                "data": [
                    {"V": ["0"], "L": "Current Position", "N": "CURRENT_POS"},
                    {"V": ["9"], "L": "Available Funds", "N": "CURRENT_FUNDS"},
                    {"V": ["7"], "L": "Post Trade Available Funds*", "N": "AFTER_FUNDS"},
                ],
            },
        )
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    preview = await broker.preview_order(
        OrderRequest(symbol="aapl", side=OrderSide.BUY, cash_qty=Decimal("2"))
    )

    assert preview.symbol == "AAPL"
    assert preview.commission == Decimal("0.02")
    assert preview.amount == Decimal("2.02")
    assert preview.margin_change is None
    assert preview.equity_change is None
    assert preview.available_funds_before == Decimal("9")
    assert preview.available_funds_after == Decimal("7")
    assert preview.warnings[0].startswith("Market Order Confirmation")
    assert len(preview.warnings) == 2
    await client.aclose()


@respx.mock
async def test_fractional_quantity_sell_body():
    orders = respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(200, json=[{"order_id": "10", "order_status": "Submitted"}])
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    result = await broker.place_order(
        OrderRequest(symbol="AAPL", side=OrderSide.SELL, quantity=Decimal("0.0066"))
    )

    assert result.order_id == "10"
    order = _sent_order(orders)
    assert order["quantity"] == 0.0066
    assert order["side"] == "SELL"
    assert "cashQty" not in order
    await client.aclose()
