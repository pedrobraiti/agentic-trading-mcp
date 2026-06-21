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
async def test_unknown_warning_blocks_order():
    respx.post(f"{BASE}/iserver/account/{ACCT}/orders").mock(
        return_value=httpx.Response(
            200, json=[{"id": "q1", "message": ["Risco alto!"], "messageIds": ["o999"]}]
        )
    )
    client = CpapiClient(BASE)
    broker = CpapiBroker(client, ACCT, _resolver)

    with pytest.raises(CpapiError, match="bloqueada"):
        await broker.place_order(
            OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("50"))
        )
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
