"""Market and account reads via CCXT: quote, balance, positions, held quantity.

Spot semantics: a "position" is simply a non-zero balance of a base asset (CCXT's
``fetch_positions`` is for margin/swap and is deliberately NOT used here).
"""

from __future__ import annotations

from decimal import Decimal

from trading_core.domain.models import AccountSummary, Position, Quote

from .client import CcxtClient, to_decimal


class CcxtMarketData:
    """Implements ``MarketDataPort`` on top of a CCXT exchange (spot)."""

    def __init__(self, client: CcxtClient):
        self._client = client

    @property
    def _ex(self):
        return self._client.exchange

    async def resolve_conid(self, symbol: str) -> int | None:
        # Crypto has no conid; the guard sizes exits via ``held_quantity`` instead.
        return None

    async def get_quote(self, symbol: str) -> Quote | None:
        await self._client.ensure_markets()
        normalized = self._client.normalize_symbol(symbol)
        ticker = await self._ex.fetch_ticker(normalized)
        return Quote(
            symbol=normalized,
            last_price=to_decimal(ticker.get("last") or ticker.get("close")),
            bid=to_decimal(ticker.get("bid")),
            ask=to_decimal(ticker.get("ask")),
        )

    async def get_quotes(self, symbols: list[str]) -> list[Quote]:
        await self._client.ensure_markets()
        normalized = [self._client.normalize_symbol(s) for s in symbols]
        tickers = await self._ex.fetch_tickers(normalized)
        quotes: list[Quote] = []
        for symbol in normalized:
            ticker = tickers.get(symbol)
            if not ticker:
                continue
            quotes.append(
                Quote(
                    symbol=symbol,
                    last_price=to_decimal(ticker.get("last") or ticker.get("close")),
                    bid=to_decimal(ticker.get("bid")),
                    ask=to_decimal(ticker.get("ask")),
                )
            )
        return quotes

    async def get_account_summary(self) -> AccountSummary:
        balance = await self._ex.fetch_balance()
        quote_ccy = self._client.quote_currency
        free = to_decimal((balance.get("free") or {}).get(quote_ccy))
        return AccountSummary(
            account_id=self._client.exchange_id,
            available_funds=free,
            net_liquidation=None,  # a full valuation would require pricing every asset
            buying_power=free,
            currency=quote_ccy,
        )

    async def get_positions(self) -> list[Position]:
        balance = await self._ex.fetch_balance()
        totals = balance.get("total") or {}
        quote_ccy = self._client.quote_currency
        positions: list[Position] = []
        for asset, amount in totals.items():
            quantity = to_decimal(amount)
            # Cash in the quote currency is "available funds", not a position.
            if quantity and quantity != 0 and asset != quote_ccy:
                positions.append(Position(symbol=asset, quantity=quantity))
        return positions

    async def held_quantity(self, symbol: str) -> Decimal | None:
        """Total base-asset balance for ``symbol`` (spot is never negative), or None."""
        return await self._base_balance(symbol, "total")

    async def free_quantity(self, symbol: str) -> Decimal | None:
        """Free (sellable NOW) base-asset balance for ``symbol``, or None.

        The exchange moves the balance a resting order holds (e.g. an untriggered stop-limit
        SELL) out of ``free`` while it stays in ``total`` — so ``free < total`` means part of
        the position is locked by open orders and a sell of the total would be rejected.
        """
        return await self._base_balance(symbol, "free")

    async def _base_balance(self, symbol: str, bucket: str) -> Decimal | None:
        await self._client.ensure_markets()
        normalized = self._client.normalize_symbol(symbol)
        try:
            base = self._client.market(normalized)["base"]
        except Exception:  # noqa: BLE001 - unknown symbol → can't confirm holdings
            return None
        balance = await self._ex.fetch_balance()
        amount = to_decimal((balance.get(bucket) or {}).get(base))
        return amount if amount is not None else Decimal(0)

    async def invalidate_positions(self) -> None:
        # fetch_balance is always a fresh REST read; nothing to invalidate.
        return None
