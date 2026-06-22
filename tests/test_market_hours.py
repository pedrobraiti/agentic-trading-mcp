from datetime import datetime
from zoneinfo import ZoneInfo

from ibkr_agent.safety.market_hours import is_market_open_at

ET = ZoneInfo("America/New_York")


def test_open_on_regular_weekday_during_hours():
    # Segunda-feira, 10:00 ET — dia comum de pregão.
    assert is_market_open_at(datetime(2026, 6, 22, 10, 0, tzinfo=ET)) is True


def test_closed_before_open():
    assert is_market_open_at(datetime(2026, 6, 22, 9, 0, tzinfo=ET)) is False


def test_closed_after_close():
    assert is_market_open_at(datetime(2026, 6, 22, 16, 0, tzinfo=ET)) is False


def test_closed_on_weekend():
    # Sábado.
    assert is_market_open_at(datetime(2026, 6, 20, 12, 0, tzinfo=ET)) is False


def test_closed_on_nyse_holiday():
    # Quinta-feira, mas é Ano Novo (feriado da NYSE) — fechado mesmo em horário de pregão.
    assert is_market_open_at(datetime(2026, 1, 1, 12, 0, tzinfo=ET)) is False
