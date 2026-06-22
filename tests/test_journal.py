from decimal import Decimal

from ibkr_agent.domain.models import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    TradingMode,
)
from ibkr_agent.journal import TradeJournal

PLACED = OrderResult(order_id="1", status=OrderStatus.SUBMITTED, symbol="AAPL", side=OrderSide.BUY)


def test_record_and_read(tmp_path):
    journal = TradeJournal(tmp_path / "trades.jsonl")
    request = OrderRequest(symbol="aapl", side=OrderSide.BUY, cash_qty=Decimal("50"))

    journal.record(
        request=request, mode=TradingMode.PAPER, dry_run=False,
        notional=Decimal("50"), result=PLACED,
    )

    rows = journal.read()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["side"] == "BUY"
    assert rows[0]["order_id"] == "1"
    assert rows[0]["notional"] == "50"


def test_spent_today_counts_only_placed_buys(tmp_path):
    journal = TradeJournal(tmp_path / "trades.jsonl")
    buy = OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("30"))

    journal.record(request=buy, mode=TradingMode.LIVE, dry_run=False,
                   notional=Decimal("30"), result=PLACED)
    journal.record(request=buy, mode=TradingMode.LIVE, dry_run=False,
                   notional=Decimal("20"), result=PLACED)
    # A dry-run has no order_id and must NOT count toward the daily spend.
    dry = OrderResult(status=OrderStatus.PENDING, symbol="AAPL", side=OrderSide.BUY, dry_run=True)
    journal.record(request=buy, mode=TradingMode.LIVE, dry_run=True,
                   notional=Decimal("999"), result=dry)

    assert journal.spent_today() == Decimal("50")


def test_has_recent_duplicate(tmp_path):
    journal = TradeJournal(tmp_path / "trades.jsonl")
    request = OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("10"))

    assert journal.has_recent_duplicate(request, 5) is False
    journal.record(request=request, mode=TradingMode.LIVE, dry_run=False,
                   notional=Decimal("10"), result=PLACED)

    assert journal.has_recent_duplicate(request, 5) is True
    # A different size is not a duplicate.
    other = OrderRequest(symbol="AAPL", side=OrderSide.BUY, cash_qty=Decimal("11"))
    assert journal.has_recent_duplicate(other, 5) is False
    # Window 0 disables the check.
    assert journal.has_recent_duplicate(request, 0) is False
