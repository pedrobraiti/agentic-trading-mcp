import json
from decimal import Decimal

import httpx
import pytest
import respx

from ibkr_agent.adapters.cpapi import CpapiBroker, CpapiClient, CpapiError
from ibkr_agent.domain.models import OrderRequest, OrderSide

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
    assert result.status.value == "cancelled"
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
