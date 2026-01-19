from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from funding_bot.config.settings import Settings
from funding_bot.services.market_data import MarketDataService


class _DummyExchange:
    def __init__(self) -> None:
        self.subscribe_orderbook_l1 = AsyncMock()


@pytest.mark.asyncio
async def test_market_data_streams_use_explicit_allowlist_and_cap():
    settings = Settings(
        websocket={
            "market_data_streams_enabled": True,
            "market_data_streams_symbols": ["ETH", "BTC"],
            "market_data_streams_max_symbols": 1,
            "orderbook_stream_depth": 1,
        }
    )
    lighter = _DummyExchange()
    x10 = _DummyExchange()
    mds = MarketDataService(settings, lighter=lighter, x10=x10)
    mds._common_symbols = {"ETH", "BTC"}

    await mds._start_market_data_streams()

    lighter.subscribe_orderbook_l1.assert_awaited_once()
    args, kwargs = lighter.subscribe_orderbook_l1.call_args
    assert args[0] == ["ETH"]
    assert kwargs["depth"] == 1


@pytest.mark.asyncio
async def test_market_data_streams_enabled_without_symbols_is_noop():
    settings = Settings(websocket={"market_data_streams_enabled": True})
    lighter = _DummyExchange()
    x10 = _DummyExchange()
    mds = MarketDataService(settings, lighter=lighter, x10=x10)
    mds._common_symbols = {"ETH"}

    await mds._start_market_data_streams()

    lighter.subscribe_orderbook_l1.assert_not_called()
