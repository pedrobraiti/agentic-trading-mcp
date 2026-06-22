"""Horário de pregão dos EUA (regular trading hours).

Fracionário só executa em RTH, e em geral não queremos disparar MKT com o mercado
fechado. Considera dia-da-semana, janela de horário no fuso do mercado e os
**feriados da NYSE** (via biblioteca ``holidays``).

Limitação conhecida: pregões de **fechamento antecipado** (meios-expedientes ~13:00
ET, ex.: véspera de Natal) não são tratados — o calendário só marca fechamentos
totais. Nesses dias a IBKR ainda barra ordens após o fechamento real.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import holidays

# Calendário da NYSE. Gera os anos sob demanda; reusar uma instância é o padrão
# eficiente recomendado pela própria lib.
_NYSE_HOLIDAYS = holidays.NYSE()


def is_market_open_at(
    moment: datetime,
    open_time: str = "09:30",
    close_time: str = "16:00",
) -> bool:
    """RTH para um instante já no fuso do mercado (função pura, testável sem relógio)."""
    if moment.weekday() > 4:  # 5=sábado, 6=domingo
        return False
    if moment.date() in _NYSE_HOLIDAYS:
        return False
    return time.fromisoformat(open_time) <= moment.time() < time.fromisoformat(close_time)


def is_market_open_now(
    tz_name: str = "America/New_York",
    open_time: str = "09:30",
    close_time: str = "16:00",
) -> bool:
    return is_market_open_at(datetime.now(ZoneInfo(tz_name)), open_time, close_time)
