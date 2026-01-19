from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from funding_bot.config.settings import Settings
from funding_bot.services.market_data import MarketDataService, OrderbookSnapshot


class _DummyExchange:
    def __init__(self, *, orderbook_sequences: dict[str, list[object]]):
        self._orderbook_sequences = orderbook_sequences
        self._orderbook_cache: dict[str, dict[str, Decimal]] = {}
        self._price_cache: dict[str, Decimal] = {}
        self._funding_cache: dict[str, object] = {}
        self._markets: dict[str, object] = {}
        self.calls: list[str] = []

    async def get_orderbook_l1(self, symbol: str) -> dict[str, Decimal]:
        self.calls.append(symbol)
        seq = self._orderbook_sequences.get(symbol, [])
        if not seq:
            return {}
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, dict)
        return item


@pytest.mark.asyncio
async def test_get_fresh_orderbook_retries_until_depth_valid():
    settings = Settings(
        websocket={
            "orderbook_l1_retry_attempts": 2,
            "orderbook_l1_retry_delay_seconds": Decimal("0"),
            "orderbook_l1_fallback_max_age_seconds": Decimal("10.0"),
        }
    )

    lighter = _DummyExchange(
        orderbook_sequences={
            "ETH": [
                {
                    "best_bid": Decimal("100"),
                    "best_ask": Decimal("101"),
                    "bid_qty": Decimal("5"),
                    "ask_qty": Decimal("6"),
                }
            ]
        }
    )
    x10 = _DummyExchange(
        orderbook_sequences={
            "ETH-USD": [
                {
                    "best_bid": Decimal("99"),
                    "best_ask": Decimal("102"),
                    "bid_qty": Decimal("0"),
                    "ask_qty": Decimal("0"),
                },
                {
                    "best_bid": Decimal("99"),
                    "best_ask": Decimal("102"),
                    "bid_qty": Decimal("10"),
                    "ask_qty": Decimal("11"),
                },
            ]
        }
    )

    mds = MarketDataService(settings, lighter=lighter, x10=x10)
    ob = await mds.get_fresh_orderbook("ETH")

    assert ob.x10_bid_qty > 0
    assert ob.x10_ask_qty > 0
    assert ob.lighter_updated is not None
    assert ob.x10_updated is not None
    assert x10.calls.count("ETH-USD") == 2


@pytest.mark.asyncio
async def test_get_fresh_orderbook_uses_recent_fallback_snapshot():
    now = datetime.now(UTC)
    settings = Settings(
        websocket={
            "orderbook_l1_retry_attempts": 1,
            "orderbook_l1_retry_delay_seconds": Decimal("0"),
            "orderbook_l1_fallback_max_age_seconds": Decimal("10.0"),
        }
    )

    lighter = _DummyExchange(
        orderbook_sequences={
            "ETH": [
                {
                    "best_bid": Decimal("100"),
                    "best_ask": Decimal("101"),
                    "bid_qty": Decimal("0"),
                    "ask_qty": Decimal("0"),
                }
            ]
        }
    )
    x10 = _DummyExchange(
        orderbook_sequences={
            "ETH-USD": [
                {
                    "best_bid": Decimal("99"),
                    "best_ask": Decimal("102"),
                    "bid_qty": Decimal("0"),
                    "ask_qty": Decimal("0"),
                }
            ]
        }
    )

    mds = MarketDataService(settings, lighter=lighter, x10=x10)
    mds._orderbooks["ETH"] = OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("100"),
        lighter_ask=Decimal("101"),
        x10_bid=Decimal("99"),
        x10_ask=Decimal("102"),
        lighter_bid_qty=Decimal("5"),
        lighter_ask_qty=Decimal("6"),
        x10_bid_qty=Decimal("10"),
        x10_ask_qty=Decimal("11"),
        lighter_updated=now - timedelta(seconds=1),
        x10_updated=now - timedelta(seconds=1),
    )

    ob = await mds.get_fresh_orderbook("ETH")
    assert ob.lighter_bid_qty > 0
    assert ob.x10_bid_qty > 0


@pytest.mark.asyncio
async def test_refresh_symbol_does_not_overwrite_depth_with_zero_qty():
    settings = Settings()
    lighter = _DummyExchange(orderbook_sequences={})
    x10 = _DummyExchange(orderbook_sequences={})

    lighter._price_cache["ETH"] = Decimal("100")
    x10._price_cache["ETH-USD"] = Decimal("100")

    lighter._orderbook_cache["ETH"] = {
        "best_bid": Decimal("100"),
        "best_ask": Decimal("101"),
        "bid_qty": Decimal("5"),
        "ask_qty": Decimal("6"),
    }
    x10._orderbook_cache["ETH-USD"] = {
        "best_bid": Decimal("99"),
        "best_ask": Decimal("102"),
        "bid_qty": Decimal("0"),
        "ask_qty": Decimal("0"),
    }

    mds = MarketDataService(settings, lighter=lighter, x10=x10)
    mds._symbol_map_to_x10["ETH"] = "ETH-USD"

    mds._orderbooks["ETH"] = OrderbookSnapshot(
        symbol="ETH",
        lighter_bid=Decimal("100"),
        lighter_ask=Decimal("101"),
        x10_bid=Decimal("99"),
        x10_ask=Decimal("102"),
        lighter_bid_qty=Decimal("5"),
        lighter_ask_qty=Decimal("6"),
        x10_bid_qty=Decimal("10"),
        x10_ask_qty=Decimal("11"),
        lighter_updated=datetime.now(UTC),
        x10_updated=datetime.now(UTC),
    )

    await mds._refresh_symbol("ETH")
    ob = mds.get_orderbook("ETH")
    assert ob is not None
    assert ob.x10_bid_qty == Decimal("10")
    assert ob.x10_ask_qty == Decimal("11")
