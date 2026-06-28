"""Safety locks over order execution.

``GuardedBroker`` is a decorator over ``BrokerPort``: it applies the hard rules BEFORE
delegating to the real broker. Rules:
  1. Live only with ``allow_live=True`` (otherwise blocked).
  2. A BUY's notional must not exceed ``max_order_value`` (exits aren't value-capped).
  3. Cumulative daily spend must not exceed ``max_daily_value`` (if set).
  4. An identical order within ``duplicate_window_seconds`` is rejected (idempotency).
  5. The market must be open (RTH), if required.
  6. ``dry_run`` (default): validates everything but does NOT send the order.

Every attempt (sent, dry-run, or blocked) is written to the ``TradeJournal`` when one
is provided.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

from ..domain.models import (
    BracketRequest,
    OrderPreview,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from ..domain.ports import BrokerPort, MarketDataPort
from ..journal import TradeJournal

logger = logging.getLogger(__name__)

# After a covering BUY is waved past the value cap, the venue's holdings can keep showing the
# old (un-reduced) short for tens of seconds (IBKR portfolio lag; exchange settlement). Without
# memory, a split or repeated "cover" against that stale short would each pass and build an
# arbitrarily large LONG past the cap.
# So a symbol gets one uncapped cover per this window; further buys are capped until the
# position settles. The reservation only ever persists (fail-safe: it caps, never uncaps).
_COVER_COOLDOWN_SECONDS = 45.0


class SafetyError(Exception):
    """The order violates a safety rule — it must not be sent."""


class GuardedBroker:
    """Implements ``BrokerPort`` by wrapping another ``BrokerPort`` with safety locks."""

    def __init__(
        self,
        inner: BrokerPort,
        market_data: MarketDataPort,
        *,
        mode: TradingMode,
        allow_live: bool,
        dry_run: bool,
        max_order_value: Decimal,
        require_market_open: bool = True,
        is_market_open: Callable[[], bool] = lambda: True,
        journal: TradeJournal | None = None,
        max_daily_value: Decimal | None = None,
        duplicate_window_seconds: float = 0.0,
        symbol_allowlist: frozenset[str] = frozenset(),
        symbol_denylist: frozenset[str] = frozenset(),
        account_info_provider: Callable[[], Awaitable[dict]] | None = None,
        configured_account_id: str = "",
        allow_short: bool = False,
        venue: str = "IBKR",
        live_env_var: str = "TRADING_ALLOW_LIVE",
        mode_env_var: str = "IBKR_TRADING_MODE",
        account_env_var: str = "IBKR_ACCOUNT_ID",
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ):
        self._inner = inner
        self._market_data = market_data
        self._mode = mode
        self._allow_live = allow_live
        self._dry_run = dry_run
        self._max_order_value = max_order_value
        self._require_market_open = require_market_open
        self._is_market_open = is_market_open
        self._journal = journal
        self._max_daily_value = max_daily_value
        self._duplicate_window_seconds = duplicate_window_seconds
        self._symbol_allowlist = symbol_allowlist
        self._symbol_denylist = symbol_denylist
        self._account_info_provider = account_info_provider
        self._configured_account_id = configured_account_id
        self._allow_short = allow_short
        self._venue = venue
        self._live_env_var = live_env_var
        self._mode_env_var = mode_env_var
        self._account_env_var = account_env_var
        self._id_factory = id_factory
        # Last account identity that passed every check — used as a fallback so a transient
        # failure reading the account doesn't trap an exit (see _verify_account).
        self._verified_identity: dict | None = None
        # normalized symbol -> monotonic time of the last uncapped covering buy
        # (see _COVER_COOLDOWN). Keyed by symbol so it is venue-agnostic (no conid).
        self._recent_covers: dict[str, float] = {}
        # Serializes the check->dispatch->journal critical section per SIDE: the journal
        # guards (daily cap is BUY-only; the duplicate guard matches on side) only ever
        # conflict same-side, so one lock per side closes the TOCTOU without making an
        # urgent exit (SELL) wait behind a slow entry (BUY).
        self._order_locks = {
            OrderSide.BUY: asyncio.Lock(),
            OrderSide.SELL: asyncio.Lock(),
        }

    async def place_order(self, request: OrderRequest) -> OrderResult:
        async with self._order_locks[request.side]:
            return await self._place_order_locked(request)

    async def _place_order_locked(self, request: OrderRequest) -> OrderResult:
        notional: Decimal | None = None
        sent = False
        coid: str | None = None
        try:
            notional = await self._run_guards(request)
            if self._dry_run:
                result = OrderResult(
                    status=OrderStatus.PENDING,
                    symbol=request.symbol.upper(),
                    side=request.side,
                    dry_run=True,
                    message=f"dry-run: order validated, NOT sent (notional ~US${notional}).",
                )
                self._record(request, notional, result=result, sent=False)
                return result

            # Thread a client order id (cOID) and journal the INTENT to dispatch BEFORE the
            # broker call. If we crash between the dispatch and recording the outcome, this
            # intent is already durable on disk, so the order is visible to the spend cap and
            # the idempotency guard on the next run (it would otherwise be invisible).
            coid = request.client_order_id or self._id_factory()
            request = request.model_copy(update={"client_order_id": coid})
            self._record_intent(request, notional, coid)
            sent = True  # dispatched to the broker — may fill even if this call errors
            result = await self._inner.place_order(request)
            self._record(request, notional, result=result, sent=sent, client_order_id=coid)
            return result
        except Exception as exc:  # noqa: BLE001 - record the failed attempt, then re-raise
            self._record(request, notional, error=exc, sent=sent, client_order_id=coid)
            raise

    async def place_bracket(self, bracket: BracketRequest) -> list[OrderResult]:
        async with self._order_locks[bracket.entry.side]:
            return await self._place_bracket_locked(bracket)

    async def _place_bracket_locked(self, bracket: BracketRequest) -> list[OrderResult]:
        # The entry is the order that spends money; guard it. The exits are part of
        # the same submission and only activate after the entry fills.
        entry = bracket.entry
        notional: Decimal | None = None
        sent = False
        coid: str | None = None
        try:
            notional = await self._run_guards(entry)
            await self._check_bracket_exits(bracket)
            if self._dry_run:
                results = [
                    OrderResult(
                        status=OrderStatus.PENDING,
                        symbol=entry.symbol.upper(),
                        side=entry.side,
                        dry_run=True,
                        message=f"dry-run: bracket validated, NOT sent (notional ~US${notional}).",
                    )
                ]
                self._record(entry, notional, result=results[0], sent=False)
                return results

            coid = entry.client_order_id or self._id_factory()
            entry = entry.model_copy(update={"client_order_id": coid})
            bracket = bracket.model_copy(update={"entry": entry})
            self._record_intent(entry, notional, coid)
            sent = True
            results = await self._inner.place_bracket(bracket)
            self._record(
                entry, notional, result=results[0] if results else None, sent=sent,
                client_order_id=coid,
            )
            return results
        except Exception as exc:  # noqa: BLE001 - record the failed attempt, then re-raise
            self._record(entry, notional, error=exc, sent=sent, client_order_id=coid)
            raise

    async def _run_guards(self, request: OrderRequest) -> Decimal | None:
        """Apply every safety lock and return the estimated notional. Raises on violation."""
        # Ground-truth account check FIRST: bind the real-money lock to the venue's reported
        # paper/live status (and the configured account to the logged-in one), not just the
        # config label.
        await self._verify_account()

        # Defense-in-depth: the label-level lock still applies even if the account
        # check above is unavailable (no provider wired).
        if self._mode is TradingMode.LIVE and not self._allow_live:
            raise SafetyError(
                f"LIVE mode blocked: set {self._live_env_var}=true to trade with real money."
            )

        symbol = request.symbol.upper()
        if symbol in self._symbol_denylist:
            raise SafetyError(f"Symbol {symbol} is on the deny-list.")
        if self._symbol_allowlist and symbol not in self._symbol_allowlist:
            raise SafetyError(f"Symbol {symbol} is not on the allow-list.")

        if self._require_market_open and not self._is_market_open():
            raise SafetyError(
                "Market closed: orders are only accepted during regular trading hours (RTH)."
            )

        # The value limit caps how much is *spent* — it applies to entries (BUYS).
        # Exits reduce exposure, so they are not value-gated and never priced (pricing
        # could RAISE on a missing quote and trap the exit). A SELL is always an exit;
        # a BUY is an exit only when it covers a SHORT (close_position emits a BUY to
        # close a short). So a covering BUY is treated like any other exit.
        notional: Decimal | None = None
        if request.side is OrderSide.BUY and not await self._is_covering_short(request):
            notional = await self._notional(request)
            if notional is not None and notional > self._max_order_value:
                raise SafetyError(
                    f"Order of ~US${notional} exceeds the MAX_ORDER_VALUE limit "
                    f"(US${self._max_order_value})."
                )

        await self._check_no_naked_short(request)
        await self._check_stop_not_inverted(request)

        if self._journal is not None and self._journal.has_recent_duplicate(
            request, self._duplicate_window_seconds
        ):
            raise SafetyError(
                f"Duplicate order blocked: an identical {request.side.value} "
                f"{request.symbol.upper()} was just placed "
                f"(within {self._duplicate_window_seconds:g}s)."
            )

        # Persistent idempotency (across processes / beyond the duplicate window): refuse to
        # resend an identical intent whose previous dispatch was never confirmed (timeout /
        # crash). This closes the retry-loop gap the short duplicate window misses — a 30-60s
        # backoff would otherwise place a SECOND live order. The block lifts only once the
        # in-flight order's fate is reconciled (order_status / open_orders / reconcile_pending).
        if self._journal is not None and self._journal.has_unresolved_dispatch(request):
            raise SafetyError(
                f"In-flight order unresolved: a previous {request.side.value} "
                f"{request.symbol.upper()} was dispatched but never confirmed (timeout or "
                "crash), so resending now could place a DUPLICATE live order. Reconcile it "
                "first (order_status / open_orders / reconcile_pending), then retry."
            )

        self._check_daily_limit(request, notional)
        return notional

    async def _verify_account(self) -> None:
        """Make the real account — not the config label — authoritative for money risk.

        The venue's reported paper/live status is the ground truth. We refuse to trade when
        the configured account doesn't match the logged-in one, when a LIVE (real-money)
        account isn't explicitly armed, or when the trading-mode label disagrees with reality.
        Every mismatch fails CLOSED, so a mislabelled setup can never quietly move real
        money. Skipped only when no account provider is wired (e.g. unit tests).
        """
        if self._account_info_provider is None:
            return
        try:
            info = await self._account_info_provider()
        except Exception as exc:  # noqa: BLE001 - tolerate a transient blip iff already verified
            # An exit must never be trapped by a momentary failure reading the account. If
            # we have a previously CONFIRMED identity, reuse it; otherwise fail closed.
            if self._verified_identity is None:
                raise SafetyError(
                    f"Could not read the account from {self._venue} to confirm paper-vs-live, "
                    "and there is no prior confirmation. Refusing to trade until it can be "
                    "confirmed."
                ) from exc
            info = self._verified_identity
        account_id = info.get("account_id")
        is_paper = info.get("is_paper")

        # Fail CLOSED on any identity uncertainty — not knowing is itself a stop signal.
        if self._configured_account_id and not account_id:
            raise SafetyError(
                f"Could not read the logged-in account from {self._venue}. "
                "Refusing to trade until the account can be confirmed."
            )
        if (
            self._configured_account_id
            and account_id
            and account_id != self._configured_account_id
        ):
            raise SafetyError(
                f"Account mismatch: configured {self._account_env_var}="
                f"{self._configured_account_id} but {self._venue} is logged into {account_id}. "
                "Refusing to trade until they match — fix .env or log in to the intended account."
            )
        if is_paper is None:
            raise SafetyError(
                f"Could not confirm from {self._venue} whether this is a PAPER or a LIVE "
                "(real-money) account. Refusing to trade — verify the login before retrying."
            )

        if is_paper is False:  # a real-money account
            if not self._allow_live:
                raise SafetyError(
                    f"LIVE account detected (real money) but {self._live_env_var} is not true. "
                    f"Refusing to send the order. Only set {self._live_env_var}=true if you "
                    "really mean to trade with real money."
                )
            if self._mode is not TradingMode.LIVE:
                raise SafetyError(
                    f"Config disagrees with reality: {self._venue} is on a LIVE (real-money) "
                    f"account but {self._mode_env_var}={self._mode.value}. Set "
                    f"{self._mode_env_var}=live so the config matches, then retry."
                )
        elif is_paper is True and self._mode is TradingMode.LIVE:
            raise SafetyError(
                f"Config disagrees with reality: {self._mode_env_var}=live but {self._venue} "
                f"is on a PAPER account. Set {self._mode_env_var}=paper to match, then retry."
            )

        # When the venue can't INDEPENDENTLY confirm paper-vs-live (e.g. a crypto exchange has
        # no authenticated testnet/mainnet flag), the "PAPER" verdict is only a config label —
        # live keys under a sandbox label would read as paper. Don't let the dry-run flag be
        # the sole barrier: a real (non-dry-run) send then needs the explicit live ack too, so
        # a silent key mismatch can't move real money. This catches a config FAILURE; the user
        # setting the ack is the deliberate opt-in. Venue-verified identities (IBKR) skip this.
        if (
            info.get("identity_verified", True) is False
            and not self._dry_run
            and not self._allow_live
        ):
            raise SafetyError(
                f"{self._venue} can't independently confirm this is a PAPER account, so its "
                f"paper/live status is unverified. A real order therefore requires "
                f"{self._live_env_var}=true as an explicit acknowledgement (a sandbox label "
                "with live keys would otherwise look like paper). Keep dry-run on, or set the "
                "ack only if you accept these keys may move real money."
            )

        # Identity confirmed — remember it so a later transient read failure can fall back
        # to it instead of trapping an exit.
        self._verified_identity = {
            "account_id": account_id, "is_paper": is_paper,
            "identity_verified": info.get("identity_verified", True),
        }

    async def _is_covering_short(self, request: OrderRequest) -> bool:
        """True if this BUY (by quantity) merely covers an existing SHORT — i.e. an exit.

        Covering a short reduces exposure, so it must not be value-capped or priced (which
        would trap the exit). If holdings can't be confirmed we return False (treat it as an
        opening buy and keep it capped — the safe default that never weakens new-buy limits).
        cashQty buys have no quantity and are always opening, so they stay capped.
        """
        if request.quantity is None:
            return False
        try:
            held = await self._market_data.held_quantity(request.symbol)
        except Exception:  # noqa: BLE001 - can't confirm a short → treat as an opening buy
            return False
        if held is None:
            return False
        # held < 0 is a short; buying up to its size covers it (a pure exit). Buying MORE
        # than the short is partly opening, so keep that capped.
        if not (held < 0 and request.quantity <= -held):
            return False
        # Allow only ONE uncapped cover per symbol per window. The position read above can be
        # stale (lag), so a second "cover" before it settles must NOT be waved through again
        # — that's how a split/repeat cover would bypass the cap. This runs under the per-
        # side lock and is synchronous (no await), so the check-and-claim is atomic.
        key = request.symbol.strip().upper()
        now = time.monotonic()
        for stale in [
            s for s, ts in self._recent_covers.items() if now - ts >= _COVER_COOLDOWN_SECONDS
        ]:
            self._recent_covers.pop(stale, None)
        if key in self._recent_covers:
            return False  # already covered this symbol recently → cap further buys (fail-safe)
        # A dry-run validates without sending, so it must not mutate gating state (it would
        # otherwise consume the one-cover allowance and trap a later real cover).
        if not self._dry_run:
            self._recent_covers[key] = now
        return True

    async def _check_no_naked_short(self, request: OrderRequest) -> None:
        """Block a SELL that exceeds the held position (which would open a short).

        Exits must never be trapped, so if holdings can't be read (infra error) the
        check is skipped rather than blocking. A genuine oversell — quantity above the
        confirmed long position — is rejected unless shorting is explicitly allowed.
        """
        if self._allow_short or request.side is not OrderSide.SELL or request.quantity is None:
            return
        try:
            held = await self._market_data.held_quantity(request.symbol)
        except Exception:  # noqa: BLE001 - can't verify holdings; never trap an exit
            return
        if held is None:
            return  # can't identify/confirm the instrument → don't trap the exit
        # Only the LONG portion can back a sell: a short (held < 0) backs nothing, so a sell
        # of any size against it opens/deepens a short. max(held, 0) captures both cases.
        held_long = held if held > Decimal(0) else Decimal(0)
        if request.quantity > held_long:
            raise SafetyError(
                f"Sell of {request.quantity} {request.symbol.upper()} exceeds the held "
                f"position ({held_long}) — this would open a short. Reduce the quantity (or use "
                "close_position to exit fully), or set TRADING_ALLOW_SHORT=true to allow it."
            )

    async def _check_stop_not_inverted(self, request: OrderRequest) -> None:
        """Reject a stop already on the wrong side of the market (it would fire instantly).

        A real stop-loss sits BELOW the market for a SELL and ABOVE it for a BUY; the
        inverse is almost always a fat-finger. Skipped if no price is available.
        """
        if request.order_type not in (OrderType.STOP, OrderType.STOP_LIMIT):
            return
        if request.stop_price is None:
            return
        try:
            quote = await self._market_data.get_quote(request.symbol)
        except Exception:  # noqa: BLE001 - no price to sanity-check against; allow it
            return
        last = quote.last_price if quote else None
        if last is None:
            return
        if request.side is OrderSide.SELL and request.stop_price >= last:
            raise SafetyError(
                f"SELL stop at {request.stop_price} is at/above the current price ({last}); "
                "it would trigger immediately. A stop-loss on a long position sits BELOW "
                "the market — check the price."
            )
        if request.side is OrderSide.BUY and request.stop_price <= last:
            raise SafetyError(
                f"BUY stop at {request.stop_price} is at/below the current price ({last}); "
                "it would trigger immediately. A buy-stop sits ABOVE the market — check the price."
            )

    async def _check_bracket_exits(self, bracket: BracketRequest) -> None:
        """Reject a bracket whose stop-loss exit would fire the instant the entry fills.

        ``BracketRequest`` already enforces take-profit/stop-loss are on the correct
        sides of each other; here we add the market-relative check (a BUY entry's SELL
        stop-loss must sit below the current price, and vice-versa). Skipped if no price.
        """
        entry = bracket.entry
        exit_is_sell = entry.side is OrderSide.BUY
        try:
            quote = await self._market_data.get_quote(entry.symbol)
        except Exception:  # noqa: BLE001 - no price to sanity-check against; allow it
            return
        last = quote.last_price if quote else None
        if last is None:
            return
        # Effective fill price. A non-marketable LIMIT entry fills at its limit (a "buy the
        # dip"); a MARKETABLE limit (BUY limit >= market) or a MARKET entry fills at ~the
        # live price. Using min(limit, last) for a BUY (max for a SELL) catches the
        # marketable case while still allowing the legit dip bracket.
        if entry.limit_price is not None:
            fill = (
                min(entry.limit_price, last)
                if entry.side is OrderSide.BUY
                else max(entry.limit_price, last)
            )
        else:
            fill = last
        stop_loss = bracket.stop_loss_price
        take_profit = bracket.take_profit_price
        # Both exits must sit on the FAR side of the effective fill, or they fill instantly
        # when the entry fills and round-trip the position. For a BUY entry the exits are
        # SELLs: take-profit ABOVE, stop-loss BELOW. A SELL entry is the mirror image.
        if exit_is_sell:
            if stop_loss >= fill:
                raise SafetyError(
                    f"Bracket stop-loss {stop_loss} is at/above the entry fill (~{fill}); the "
                    "SELL stop would trigger immediately when the entry fills — check the levels."
                )
            if take_profit <= fill:
                raise SafetyError(
                    f"Bracket take-profit {take_profit} is at/below the entry fill (~{fill}); "
                    "the SELL limit would fill immediately when the entry fills — check the levels."
                )
        else:
            if stop_loss <= fill:
                raise SafetyError(
                    f"Bracket stop-loss {stop_loss} is at/below the entry fill (~{fill}); the "
                    "BUY stop would trigger immediately when the entry fills — check the levels."
                )
            if take_profit >= fill:
                raise SafetyError(
                    f"Bracket take-profit {take_profit} is at/above the entry fill (~{fill}); "
                    "the BUY limit would fill immediately when the entry fills — check the levels."
                )

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        # Read-only estimate (margin/commission/warnings); no guard needed.
        return await self._inner.preview_order(request)

    async def get_order_status(self, order_id: str) -> OrderResult:
        return await self._inner.get_order_status(order_id)

    async def cancel_order(self, order_id: str) -> OrderResult:
        return await self._inner.cancel_order(order_id)

    async def get_live_orders(self) -> list[OrderResult]:
        return await self._inner.get_live_orders()

    def _check_daily_limit(self, request: OrderRequest, notional: Decimal | None) -> None:
        if self._max_daily_value is None or self._journal is None:
            return
        if request.side is not OrderSide.BUY or notional is None:
            return
        spent = self._journal.spent_today()
        if spent + notional > self._max_daily_value:
            remaining = self._max_daily_value - spent
            raise SafetyError(
                f"Daily spend limit reached: ~US${spent} already spent today, this order is "
                f"~US${notional}, limit is US${self._max_daily_value} (remaining US${remaining})."
            )

    def _record(
        self,
        request: OrderRequest,
        notional: Decimal | None,
        *,
        result: OrderResult | None = None,
        error: Exception | None = None,
        sent: bool = False,
        client_order_id: str | None = None,
    ) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record(
                request=request,
                mode=self._mode,
                dry_run=self._dry_run,
                notional=notional,
                result=result,
                error=error,
                sent=sent,
                client_order_id=client_order_id,
            )
        except Exception:  # noqa: BLE001 - journaling must never break trading
            logger.warning("Failed to journal an order attempt", exc_info=True)

    def _record_intent(
        self, request: OrderRequest, notional: Decimal | None, client_order_id: str
    ) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record_intent(
                request=request,
                mode=self._mode,
                notional=notional,
                client_order_id=client_order_id,
            )
        except Exception:  # noqa: BLE001 - journaling must never break trading
            logger.warning("Failed to journal an order intent", exc_info=True)

    async def _notional(self, request: OrderRequest) -> Decimal | None:
        """Estimated order value in US$. For cashQty it's direct; for quantity it uses the quote."""
        if request.cash_qty is not None:
            return request.cash_qty

        quote = await self._market_data.get_quote(request.symbol)
        price = quote.last_price if quote else None
        # A non-positive price (a zero/garbage tick) would make the notional 0 and silently
        # bypass the value cap — treat it like a missing price and fail closed.
        if price is None or price <= 0:
            raise SafetyError(
                f"No usable price for {request.symbol} (got {price}): cannot validate the "
                "order's value limit."
            )
        return price * (request.quantity or Decimal(0))
