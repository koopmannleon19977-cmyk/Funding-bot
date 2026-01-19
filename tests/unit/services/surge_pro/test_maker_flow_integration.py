"""
Integration test for Surge Pro maker flow.

This tests the full entry -> exit cycle using mocks to verify component wiring
without requiring live API credentials.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from funding_bot.services.surge_pro.maker_engine import SurgeProMakerEngine


@pytest.mark.asyncio
async def test_full_maker_cycle():
    """Test complete entry -> exit cycle with maker orders."""
    # Setup mocks
    surge_pro_settings = MagicMock()
    surge_pro_settings.order_mode = "maker"
    surge_pro_settings.symbols = ["BTC"]
    surge_pro_settings.entry_imbalance_threshold = Decimal("0.25")
    surge_pro_settings.exit_imbalance_threshold = Decimal("0.10")
    surge_pro_settings.max_spread_bps = Decimal("10")
    surge_pro_settings.maker_entry_timeout_s = 0.5
    surge_pro_settings.maker_exit_timeout_s = 0.5
    surge_pro_settings.maker_exit_max_retries = 2
    surge_pro_settings.max_trade_notional_usd = Decimal("200")
    surge_pro_settings.max_open_trades = 3
    surge_pro_settings.daily_loss_cap_usd = Decimal("5")
    surge_pro_settings.stop_loss_bps = Decimal("20")
    surge_pro_settings.take_profit_bps = Decimal("15")
    surge_pro_settings.cooldown_seconds = 1
    surge_pro_settings.hourly_loss_pause_usd = Decimal("2")
    surge_pro_settings.loss_streak_pause_count = 5
    surge_pro_settings.min_fill_rate_percent = 20
    surge_pro_settings.pause_duration_minutes = 30

    settings = MagicMock()
    settings.surge_pro = surge_pro_settings
    settings.live_trading = True

    x10 = AsyncMock()
    x10._markets = {
        "BTC-USD": MagicMock(
            tick_size=Decimal("1"),
            step_size=Decimal("0.00001"),
        ),
    }

    store = AsyncMock()
    store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
    store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
    store.get_recent_trades = AsyncMock(return_value=[])
    store.get_hourly_fill_rate = AsyncMock(return_value=0.5)
    store.list_open_surge_trades = AsyncMock(return_value=[])
    store.create_surge_trade = AsyncMock()
    store.update_surge_trade = AsyncMock()

    # Entry orderbook - strong buy signal (heavy bids vs light asks)
    x10.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("10.0"))],  # Heavy bids
            "asks": [(Decimal("50005"), Decimal("1.0"))],  # Light asks
        }
    )
    x10.get_orderbook_l1 = AsyncMock(
        return_value={
            "best_bid": "50000",
            "best_ask": "50005",
        }
    )

    # Orders fill immediately
    filled_order = Order(
        order_id="123",
        symbol="BTC",
        exchange=Exchange.X10,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("0.004"),
        price=Decimal("50001"),
        time_in_force=TimeInForce.POST_ONLY,
        status=OrderStatus.FILLED,
        avg_fill_price=Decimal("50001"),
        filled_qty=Decimal("0.004"),
    )
    x10.place_order = AsyncMock(return_value=filled_order)
    x10.get_order = AsyncMock(return_value=filled_order)

    engine = SurgeProMakerEngine(settings=settings, x10=x10, store=store)

    # Run tick - should enter
    await engine.tick()

    # Verify POST_ONLY was used for entry
    assert x10.place_order.called
    call_args = x10.place_order.call_args
    request = call_args[0][0]
    assert request.time_in_force == TimeInForce.POST_ONLY

    # Verify trade was created
    store.create_surge_trade.assert_called()


@pytest.mark.asyncio
async def test_risk_guard_blocks_on_daily_loss():
    """Test that RiskGuard blocks trading when daily loss cap is hit."""
    surge_pro_settings = MagicMock()
    surge_pro_settings.symbols = ["BTC"]
    surge_pro_settings.daily_loss_cap_usd = Decimal("5")
    surge_pro_settings.hourly_loss_pause_usd = Decimal("2")
    surge_pro_settings.loss_streak_pause_count = 5
    surge_pro_settings.min_fill_rate_percent = 20
    surge_pro_settings.pause_duration_minutes = 30

    settings = MagicMock()
    settings.surge_pro = surge_pro_settings
    settings.live_trading = True

    x10 = AsyncMock()
    x10._markets = {}

    # Daily loss exceeds cap
    store = AsyncMock()
    store.get_daily_pnl = AsyncMock(return_value=Decimal("-6"))  # -$6 > $5 cap
    store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
    store.get_recent_trades = AsyncMock(return_value=[])
    store.get_hourly_fill_rate = AsyncMock(return_value=0.5)
    store.list_open_surge_trades = AsyncMock(return_value=[])

    engine = SurgeProMakerEngine(settings=settings, x10=x10, store=store)

    # Run tick - should be blocked
    await engine.tick()

    # Verify no orders were placed
    x10.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_no_entry_when_spread_too_wide():
    """Test that entries are skipped when spread exceeds threshold."""
    surge_pro_settings = MagicMock()
    surge_pro_settings.symbols = ["BTC"]
    surge_pro_settings.entry_imbalance_threshold = Decimal("0.25")
    surge_pro_settings.max_spread_bps = Decimal("5")  # Very tight spread required
    surge_pro_settings.daily_loss_cap_usd = Decimal("5")
    surge_pro_settings.hourly_loss_pause_usd = Decimal("2")
    surge_pro_settings.loss_streak_pause_count = 5
    surge_pro_settings.min_fill_rate_percent = 20
    surge_pro_settings.pause_duration_minutes = 30
    surge_pro_settings.max_open_trades = 3
    surge_pro_settings.cooldown_seconds = 1

    settings = MagicMock()
    settings.surge_pro = surge_pro_settings
    settings.live_trading = True

    x10 = AsyncMock()
    x10._markets = {
        "BTC-USD": MagicMock(
            tick_size=Decimal("1"),
            step_size=Decimal("0.00001"),
        ),
    }

    store = AsyncMock()
    store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
    store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
    store.get_recent_trades = AsyncMock(return_value=[])
    store.get_hourly_fill_rate = AsyncMock(return_value=0.5)
    store.list_open_surge_trades = AsyncMock(return_value=[])

    # Strong imbalance signal but wide spread
    x10.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("10.0"))],
            "asks": [(Decimal("50100"), Decimal("1.0"))],  # 20bps spread
        }
    )
    x10.get_orderbook_l1 = AsyncMock(
        return_value={
            "best_bid": "50000",
            "best_ask": "50100",  # 20bps spread > 5bps threshold
        }
    )

    engine = SurgeProMakerEngine(settings=settings, x10=x10, store=store)

    await engine.tick()

    # Verify no orders placed due to wide spread
    x10.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_no_entry_without_imbalance_signal():
    """Test that entries require sufficient imbalance."""
    surge_pro_settings = MagicMock()
    surge_pro_settings.symbols = ["BTC"]
    surge_pro_settings.entry_imbalance_threshold = Decimal("0.25")
    surge_pro_settings.max_spread_bps = Decimal("10")
    surge_pro_settings.daily_loss_cap_usd = Decimal("5")
    surge_pro_settings.hourly_loss_pause_usd = Decimal("2")
    surge_pro_settings.loss_streak_pause_count = 5
    surge_pro_settings.min_fill_rate_percent = 20
    surge_pro_settings.pause_duration_minutes = 30
    surge_pro_settings.max_open_trades = 3
    surge_pro_settings.cooldown_seconds = 1

    settings = MagicMock()
    settings.surge_pro = surge_pro_settings
    settings.live_trading = True

    x10 = AsyncMock()
    x10._markets = {
        "BTC-USD": MagicMock(
            tick_size=Decimal("1"),
            step_size=Decimal("0.00001"),
        ),
    }

    store = AsyncMock()
    store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
    store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
    store.get_recent_trades = AsyncMock(return_value=[])
    store.get_hourly_fill_rate = AsyncMock(return_value=0.5)
    store.list_open_surge_trades = AsyncMock(return_value=[])

    # Balanced orderbook - no signal
    x10.get_orderbook_depth = AsyncMock(
        return_value={
            "bids": [(Decimal("50000"), Decimal("5.0"))],
            "asks": [(Decimal("50005"), Decimal("5.0"))],  # Balanced
        }
    )
    x10.get_orderbook_l1 = AsyncMock(
        return_value={
            "best_bid": "50000",
            "best_ask": "50005",
        }
    )

    engine = SurgeProMakerEngine(settings=settings, x10=x10, store=store)

    await engine.tick()

    # Verify no orders placed due to no imbalance
    x10.place_order.assert_not_called()
