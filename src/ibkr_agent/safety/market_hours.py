"""Horário de pregão dos EUA (regular trading hours).

Fracionário só executa em RTH, e em geral não queremos disparar MKT com o mercado
fechado. Implementação simples por dia-da-semana + janela de horário no fuso do
mercado. Feriados ainda NÃO são tratados (TODO: integrar um calendário de mercado).
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


def is_market_open_now(
    tz_name: str = "America/New_York",
    open_time: str = "09:30",
    close_time: str = "16:00",
) -> bool:
    now = datetime.now(ZoneInfo(tz_name))
    if now.weekday() > 4:  # 5=sábado, 6=domingo
        return False
    return time.fromisoformat(open_time) <= now.time() < time.fromisoformat(close_time)
