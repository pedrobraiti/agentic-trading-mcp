"""Leitura de mercado e conta via CPAPI: conid, cotação, saldo e posições."""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

from ...domain.models import AccountSummary, Position, Quote
from .client import CpapiClient

FIELD_LAST = "31"
FIELD_BID = "84"
FIELD_ASK = "86"
_SNAPSHOT_FIELDS = f"{FIELD_LAST},{FIELD_BID},{FIELD_ASK}"
_SNAPSHOT_MAX_ATTEMPTS = 3


class CpapiMarketData:
    """Implementa ``MarketDataPort`` sobre a CPAPI."""

    def __init__(
        self,
        client: CpapiClient,
        account_id: str,
        *,
        warmup_delay_seconds: float = 1.0,
    ):
        self._client = client
        self._account_id = account_id
        self._warmup_delay = warmup_delay_seconds
        self._conid_cache: dict[str, int] = {}

    async def resolve_conid(self, symbol: str) -> int | None:
        symbol = symbol.upper()
        if symbol in self._conid_cache:
            return self._conid_cache[symbol]

        data = await self._client.get("/trsrv/stocks", params={"symbols": symbol})
        conid = _pick_us_conid(data, symbol) if isinstance(data, dict) else None
        if conid is not None:
            self._conid_cache[symbol] = conid
        return conid

    async def get_quote(self, symbol: str) -> Quote | None:
        conid = await self.resolve_conid(symbol)
        if conid is None:
            return None

        params = {"conids": str(conid), "fields": _SNAPSHOT_FIELDS}
        snapshot: dict = {}
        # Warmup: a 1ª chamada inicia o stream e volta sem preço; repetir até vir dado.
        for attempt in range(_SNAPSHOT_MAX_ATTEMPTS):
            data = await self._client.get("/iserver/marketdata/snapshot", params=params)
            if isinstance(data, list) and data and FIELD_LAST in data[0]:
                snapshot = data[0]
                break
            if attempt < _SNAPSHOT_MAX_ATTEMPTS - 1:
                await asyncio.sleep(self._warmup_delay)

        return Quote(
            symbol=symbol.upper(),
            conid=conid,
            last_price=_to_decimal(snapshot.get(FIELD_LAST)),
            bid=_to_decimal(snapshot.get(FIELD_BID)),
            ask=_to_decimal(snapshot.get(FIELD_ASK)),
        )

    async def get_account_summary(self) -> AccountSummary:
        data = await self._client.get(f"/portfolio/{self._account_id}/summary")
        data = data if isinstance(data, dict) else {}
        return AccountSummary(
            account_id=self._account_id,
            available_funds=_amount(data, "availablefunds"),
            net_liquidation=_amount(data, "netliquidation"),
            buying_power=_amount(data, "buyingpower"),
        )

    async def get_positions(self) -> list[Position]:
        positions: list[Position] = []
        page = 0
        while True:
            data = await self._client.get(f"/portfolio/{self._account_id}/positions/{page}")
            if not isinstance(data, list) or not data:
                break
            for raw in data:
                positions.append(_to_position(raw))
            page += 1
        return positions


def _pick_us_conid(data: dict, symbol: str) -> int | None:
    entries = data.get(symbol) or data.get(symbol.upper())
    if not isinstance(entries, list):
        return None
    for entry in entries:
        contracts = entry.get("contracts", []) if isinstance(entry, dict) else []
        for contract in contracts:
            if contract.get("isUS") and contract.get("conid"):
                return int(contract["conid"])
    # Fallback: primeiro conid disponível, se nenhum marcado como US.
    for entry in entries:
        for contract in entry.get("contracts", []) if isinstance(entry, dict) else []:
            if contract.get("conid"):
                return int(contract["conid"])
    return None


def _to_position(raw: dict) -> Position:
    return Position(
        conid=int(raw.get("conid", 0)),
        symbol=str(raw.get("contractDesc") or raw.get("ticker") or raw.get("symbol") or ""),
        quantity=_to_decimal(raw.get("position")) or Decimal(0),
        avg_cost=_to_decimal(raw.get("avgCost")),
        market_price=_to_decimal(raw.get("mktPrice")),
        market_value=_to_decimal(raw.get("mktValue")),
        unrealized_pnl=_to_decimal(raw.get("unrealizedPnl")),
    )


def _amount(data: dict, key: str) -> Decimal | None:
    field = data.get(key)
    raw = _to_decimal(field.get("amount")) if isinstance(field, dict) else _to_decimal(field)
    # Valores de saldo vêm como float com ruído (ex.: 8.869999...) — arredonda p/ centavos.
    return raw.quantize(Decimal("0.01")) if raw is not None else None


def _to_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
