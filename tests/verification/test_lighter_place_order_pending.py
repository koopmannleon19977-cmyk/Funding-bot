from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
from funding_bot.domain.models import Exchange, MarketInfo, OrderRequest, OrderStatus, OrderType, Side, TimeInForce


@pytest.mark.asyncio
async def test_lighter_place_order_signed_returns_pending_id_without_resolving(monkeypatch):
    from datetime import UTC, datetime

    settings = MagicMock()
    settings.lighter.base_url = "https://example.invalid"
    settings.lighter.private_key = "0xdeadbeef"
    settings.lighter.account_index = 1
    settings.lighter.api_key_index = 0
    settings.lighter.l1_address = "0xabc"

    adapter = LighterAdapter(settings)

    # Deterministic client_order_index: 1_700_000_000_000
    monkeypatch.setattr("funding_bot.adapters.exchanges.lighter.adapter.time.time", lambda: 1_700_000_000.0)
    monkeypatch.setattr("funding_bot.adapters.exchanges.lighter.adapter.random.randint", lambda _a, _b: 0)

    adapter._get_market_index = MagicMock(return_value=7)
    adapter.get_market_info = AsyncMock(
        return_value=MarketInfo(
            symbol="ETH",
            exchange=Exchange.LIGHTER,
            base_asset="ETH",
            price_precision=2,
            qty_precision=4,
        )
    )

    # Mock price cache for freshness validation (added after price freshness check implementation)
    # The validation checks if cached price is within 5 seconds, so we provide a fresh price
    adapter._price_cache["ETH"] = Decimal("100")
    adapter._price_updated_at["ETH"] = datetime.now(UTC)

    adapter._signer = MagicMock()
    adapter._signer.create_order = AsyncMock(return_value=(MagicMock(), MagicMock(tx_hash="0x1"), None))

    async def _sdk_call_with_retry(coro_func, max_retries: int = 3, **kwargs):  # noqa: ARG001
        return await coro_func()

    adapter._sdk_call_with_retry = _sdk_call_with_retry  # type: ignore[method-assign]

    adapter._resolve_order_id = AsyncMock(side_effect=AssertionError("_resolve_order_id should not be called"))  # type: ignore[method-assign]

    req = OrderRequest(
        symbol="ETH",
        exchange=Exchange.LIGHTER,
        side=Side.BUY,
        qty=Decimal("1"),
        order_type=OrderType.LIMIT,
        price=Decimal("100"),
        time_in_force=TimeInForce.POST_ONLY,
        reduce_only=False,
    )

    order = await adapter.place_order(req)

    assert order.order_id == "pending_1700000000000_7"
    assert order.status == OrderStatus.PENDING
    assert order.client_order_id == "1700000000000"

