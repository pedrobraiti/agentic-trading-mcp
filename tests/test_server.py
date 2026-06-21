from ibkr_agent.config import Settings
from ibkr_agent.server.app import mcp
from ibkr_agent.server.services import build_services


async def test_tools_are_registered():
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    expected = {
        "session_status",
        "market_status",
        "get_quote",
        "account_summary",
        "positions",
        "buy",
        "sell",
        "cancel_order",
        "open_orders",
    }
    assert expected <= names


def test_build_services_wires_without_network():
    svc = build_services(Settings(ibkr_account_id="DU1", trading_dry_run=True))
    assert svc.settings.ibkr_account_id == "DU1"
    assert isinstance(svc.market_is_open(), bool)
    assert svc.broker is not None
