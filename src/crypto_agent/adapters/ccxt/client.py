"""Thin wrapper around a CCXT async exchange instance.

Owns the single exchange object, lazily loads markets (precision/limits/minimums),
normalizes symbols to CCXT's ``BASE/QUOTE`` form, and centralizes the Decimal/float
conversions (CCXT speaks ``float``; the domain speaks ``Decimal``). The market-data and
broker adapters are built on top of one ``CcxtClient``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


class CryptoExchangeError(Exception):
    """A crypto venue / adapter level error surfaced to the agent."""


def to_decimal(value: Any) -> Decimal | None:
    """CCXT returns floats (or None); convert via ``str`` to avoid binary-float error."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None


class CcxtClient:
    """Holds the CCXT async exchange and the venue-wide helpers (symbols, limits)."""

    def __init__(
        self,
        exchange_id: str,
        *,
        api_key: str = "",
        api_secret: str = "",
        password: str = "",
        sandbox: bool = True,
        quote_currency: str = "USDT",
        exchange: object | None = None,
    ):
        # ``exchange`` lets tests inject a fake CCXT-shaped object; in production it's built
        # from ccxt (imported lazily so importing the package doesn't require ccxt).
        if exchange is not None:
            self.exchange = exchange
        else:
            import ccxt.async_support as accxt

            if not hasattr(accxt, exchange_id):
                raise CryptoExchangeError(f"Unknown CCXT exchange '{exchange_id}'.")
            self.exchange = getattr(accxt, exchange_id)(
                {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "password": password or None,
                    "enableRateLimit": True,
                }
            )
            if sandbox:
                # testnet where the exchange provides one = our paper-first mode.
                self.exchange.set_sandbox_mode(True)
        # binance (the default exchange) ships with warnOnFetchOpenOrdersWithoutSymbol=True
        # and RAISES ExchangeError on a symbol-less fetch_open_orders — which would break
        # get_live_orders, reconcile_pending and the cancel of a resting stop after a restart.
        # It is a rate-limit advisory (heavier weight per call), irrelevant for one account
        # polling occasionally, so acknowledge it once here for whatever exchange is used.
        options = getattr(self.exchange, "options", None)
        if isinstance(options, dict):
            options["warnOnFetchOpenOrdersWithoutSymbol"] = False
        self.exchange_id = exchange_id
        self.sandbox = sandbox
        self.quote_currency = quote_currency.strip().upper()
        self._markets_loaded = False

    async def ensure_markets(self) -> None:
        """Load markets once (precision, limits, minimums) — required before trading."""
        if not self._markets_loaded:
            await self.exchange.load_markets()
            self._markets_loaded = True

    def normalize_symbol(self, symbol: str) -> str:
        """Normalize to CCXT's ``BASE/QUOTE`` form, defaulting the quote currency.

        ``"btc"`` → ``"BTC/USDT"``; ``"eth/usdt"`` → ``"ETH/USDT"`` (already qualified).
        """
        normalized = symbol.strip().upper()
        if "/" not in normalized:
            normalized = f"{normalized}/{self.quote_currency}"
        return normalized

    def market(self, symbol: str) -> dict:
        """The loaded market dict for a normalized symbol (raises if unknown)."""
        markets = self.exchange.markets or {}
        if symbol not in markets:
            raise CryptoExchangeError(
                f"Symbol {symbol} is not listed on {self.exchange_id} "
                f"(spot, quote {self.quote_currency}). Check the pair."
            )
        return markets[symbol]

    def amount_to_precision(self, symbol: str, amount: Decimal) -> Decimal:
        """Round an amount to the market's allowed precision (CCXT returns a string)."""
        rounded = self.exchange.amount_to_precision(symbol, float(amount))
        return Decimal(str(rounded))

    def validate_limits(
        self, symbol: str, amount: Decimal, price: Decimal | None
    ) -> None:
        """Reject an order below the market's minimum amount or notional, with a clear message.

        Crypto venues reject sub-minimum orders server-side; catching it here turns a raw
        exchange error into an actionable one and avoids a wasted round-trip.
        """
        limits = (self.market(symbol).get("limits") or {})
        min_amount = to_decimal((limits.get("amount") or {}).get("min"))
        min_cost = to_decimal((limits.get("cost") or {}).get("min"))
        if min_amount is not None and amount < min_amount:
            raise CryptoExchangeError(
                f"Amount {amount} {symbol} is below the market minimum of {min_amount}."
            )
        if min_cost is not None and price is not None:
            cost = amount * price
            if cost < min_cost:
                raise CryptoExchangeError(
                    f"Order notional ~{cost} {self.quote_currency} is below the market "
                    f"minimum of {min_cost} {self.quote_currency}."
                )

    def validate_cost(self, symbol: str, cost: Decimal) -> None:
        """Reject a buy-by-value below the market's minimum notional."""
        limits = (self.market(symbol).get("limits") or {})
        min_cost = to_decimal((limits.get("cost") or {}).get("min"))
        if min_cost is not None and cost < min_cost:
            raise CryptoExchangeError(
                f"Buy of {cost} {self.quote_currency} is below the market minimum notional "
                f"of {min_cost} {self.quote_currency}."
            )

    async def aclose(self) -> None:
        """Close the underlying aiohttp session (CCXT async exchanges must be closed).

        Note: ccxt/aiohttp may still log a cosmetic "Unclosed client session" line at
        interpreter shutdown on Windows even after this runs — it's harmless.
        """
        await self.exchange.close()
