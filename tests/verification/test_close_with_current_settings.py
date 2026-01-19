from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.config.settings import Settings
from funding_bot.domain.models import (
    Exchange,
    MarketInfo,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Trade,
    TradeStatus,
)
from funding_bot.domain.rules import evaluate_exit_rules
from funding_bot.services.positions import PositionManager


def _load_current_settings() -> Settings:
    # Use the repo's current unified config (src/funding_bot/config/config.yaml)
    return Settings.from_yaml(env="prod")


@pytest.mark.asyncio
async def test_current_settings_early_take_profit_rule_triggers_with_effective_threshold():
    settings = _load_current_settings()

    assert settings.trading.early_take_profit_enabled is True
    assert settings.trading.early_take_profit_net_usd > 0

    trade = Trade.create(
        symbol="TEST",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("1"),
        target_notional_usd=Decimal("100"),
        entry_apy=Decimal("0.50"),
    )
    trade.status = TradeStatus.OPEN
    trade.opened_at = datetime.now(UTC) - timedelta(minutes=2)
    trade.leg1.filled_qty = Decimal("1")
    trade.leg2.filled_qty = Decimal("1")
    trade.leg1.entry_price = Decimal("100")
    trade.leg2.entry_price = Decimal("100")

    estimated_exit_cost = Decimal("0.35")
    slippage_buffer = estimated_exit_cost * settings.trading.early_take_profit_slippage_multiple
    effective_threshold = settings.trading.early_take_profit_net_usd + max(slippage_buffer, Decimal("0.50"))

    decision = evaluate_exit_rules(
        trade=trade,
        current_pnl=effective_threshold + Decimal("0.01"),
        current_apy=Decimal("0.0"),
        lighter_rate=Decimal("0"),
        x10_rate=Decimal("0"),
        min_hold_seconds=settings.trading.min_hold_seconds,
        max_hold_hours=settings.trading.max_hold_hours,
        profit_target_usd=settings.trading.min_profit_exit_usd,
        funding_flip_hours=settings.trading.funding_flip_hours_threshold,
        estimated_exit_cost_usd=estimated_exit_cost,
        price_pnl=effective_threshold + Decimal("0.01"),
        early_take_profit_enabled=settings.trading.early_take_profit_enabled,
        early_take_profit_net_usd=settings.trading.early_take_profit_net_usd,
        early_take_profit_slippage_multiple=settings.trading.early_take_profit_slippage_multiple,
        early_edge_exit_enabled=settings.trading.early_edge_exit_enabled,
        early_edge_exit_min_age_seconds=settings.trading.early_edge_exit_min_age_seconds,
        exit_ev_enabled=settings.trading.exit_ev_enabled,
        exit_ev_horizon_hours=settings.trading.exit_ev_horizon_hours,
        exit_ev_exit_cost_multiple=settings.trading.exit_ev_exit_cost_multiple,
        exit_ev_skip_profit_target_when_edge_good=settings.trading.exit_ev_skip_profit_target_when_edge_good,
        exit_ev_skip_opportunity_cost_when_edge_good=settings.trading.exit_ev_skip_opportunity_cost_when_edge_good,
    )

    assert decision.should_exit is True
    assert decision.reason.startswith("EARLY_TAKE_PROFIT:")


@pytest.mark.asyncio
async def test_current_settings_early_take_profit_closes_both_legs_taker_direct(monkeypatch):
    import funding_bot.services.positions as positions_mod

    monkeypatch.setattr(positions_mod.asyncio, "sleep", AsyncMock())

    settings = _load_current_settings()
    assert settings.trading.early_tp_fast_close_enabled is True
    assert settings.trading.early_tp_fast_close_use_taker_directly is True

    trade = Trade.create(
        symbol="TEST",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("2"),
        target_notional_usd=Decimal("200"),
        entry_apy=Decimal("0.50"),
    )
    trade.status = TradeStatus.OPEN
    trade.opened_at = datetime.now(UTC) - timedelta(minutes=5)
    trade.leg1.filled_qty = Decimal("2")
    trade.leg2.filled_qty = Decimal("2")
    trade.leg1.entry_price = Decimal("100")
    trade.leg2.entry_price = Decimal("100")

    lighter = AsyncMock()
    lighter.exchange = Exchange.LIGHTER

    x10 = AsyncMock()
    x10.exchange = Exchange.X10

    # Market data provides the X10 L1 price used for IOC close.
    market_data = MagicMock()
    market_data.get_orderbook.return_value = MagicMock(x10_bid=Decimal("99.9"), x10_ask=Decimal("100.1"))
    market_data.get_price.return_value = MagicMock(x10_price=Decimal("100.0"))
    market_data.get_market_info.return_value = MarketInfo(
        symbol="TEST",
        exchange=Exchange.X10,
        base_asset="TEST",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.0001"),
    )

    # X10 position exists initially, then is gone at verify.
    x10.get_position = AsyncMock(
        side_effect=[
            Position(
                symbol="TEST", exchange=Exchange.X10, side=trade.leg2.side, qty=Decimal("2"), entry_price=Decimal("100")
            ),
            None,  # _verify_closed()
        ]
    )

    # X10 IOC close fills immediately.
    x10.place_order = AsyncMock(
        return_value=Order(
            order_id="x10_close_1",
            symbol="TEST",
            exchange=Exchange.X10,
            side=trade.leg2.side.inverse(),
            order_type=OrderType.LIMIT,
            qty=Decimal("2"),
            price=Decimal("100.1"),
            status=OrderStatus.PENDING,
        )
    )
    x10.get_order = AsyncMock(
        return_value=Order(
            order_id="x10_close_1",
            symbol="TEST",
            exchange=Exchange.X10,
            side=trade.leg2.side.inverse(),
            order_type=OrderType.LIMIT,
            qty=Decimal("2"),
            price=Decimal("100.1"),
            status=OrderStatus.FILLED,
            filled_qty=Decimal("2"),
            avg_fill_price=Decimal("100.1"),
            fee=Decimal("0"),
        )
    )

    # Lighter market close fills immediately.
    lighter.place_order = AsyncMock(
        return_value=Order(
            order_id="l_close_1",
            symbol="TEST",
            exchange=Exchange.LIGHTER,
            side=trade.leg1.side.inverse(),
            order_type=OrderType.MARKET,
            qty=Decimal("2"),
            status=OrderStatus.FILLED,
            filled_qty=Decimal("2"),
            avg_fill_price=Decimal("99.9"),
            fee=Decimal("0"),
        )
    )
    lighter.get_order = AsyncMock(
        return_value=Order(
            order_id="l_close_1",
            symbol="TEST",
            exchange=Exchange.LIGHTER,
            side=trade.leg1.side.inverse(),
            order_type=OrderType.MARKET,
            qty=Decimal("2"),
            status=OrderStatus.FILLED,
            filled_qty=Decimal("2"),
            avg_fill_price=Decimal("99.9"),
            fee=Decimal("0"),
        )
    )
    lighter.get_position = AsyncMock(return_value=None)

    store = AsyncMock()
    event_bus = AsyncMock()

    pm = PositionManager(settings, lighter, x10, store, event_bus, market_data)
    result = await pm.close_trade(trade, "EARLY_TAKE_PROFIT: synthetic test")

    assert result.success is True
    assert trade.status == TradeStatus.CLOSED

    # Validate the high-level behavior: early-TP uses taker-direct market close on Lighter
    # and IOC close on X10 (with reduce_only=true).
    lighter.place_order.assert_awaited()
    lighter_req: OrderRequest = lighter.place_order.await_args.args[0]
    assert lighter_req.order_type == OrderType.MARKET
    assert lighter_req.reduce_only is True

    x10.place_order.assert_awaited()
    x10_req: OrderRequest = x10.place_order.await_args.args[0]
    assert x10_req.order_type == OrderType.LIMIT
    assert x10_req.reduce_only is True


@pytest.mark.asyncio
async def test_current_settings_x10_close_falls_back_to_market_on_ioc_failures(monkeypatch):
    import funding_bot.services.positions as positions_mod

    monkeypatch.setattr(positions_mod.asyncio, "sleep", AsyncMock())

    settings = _load_current_settings()

    trade = Trade.create(
        symbol="TEST",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("3"),
        target_notional_usd=Decimal("300"),
        entry_apy=Decimal("0.50"),
    )
    trade.status = TradeStatus.OPEN
    trade.opened_at = datetime.now(UTC) - timedelta(minutes=5)
    trade.leg1.filled_qty = Decimal("3")
    trade.leg2.filled_qty = Decimal("3")
    trade.leg1.entry_price = Decimal("100")
    trade.leg2.entry_price = Decimal("100")

    lighter = AsyncMock()
    lighter.exchange = Exchange.LIGHTER
    lighter.get_position = AsyncMock(return_value=None)
    lighter.place_order = AsyncMock(
        return_value=Order(
            order_id="l_close_1",
            symbol="TEST",
            exchange=Exchange.LIGHTER,
            side=trade.leg1.side.inverse(),
            order_type=OrderType.MARKET,
            qty=Decimal("3"),
            status=OrderStatus.FILLED,
            filled_qty=Decimal("3"),
            avg_fill_price=Decimal("99.9"),
            fee=Decimal("0"),
        )
    )
    lighter.get_order = AsyncMock(
        return_value=Order(
            order_id="l_close_1",
            symbol="TEST",
            exchange=Exchange.LIGHTER,
            side=trade.leg1.side.inverse(),
            order_type=OrderType.MARKET,
            qty=Decimal("3"),
            status=OrderStatus.FILLED,
            filled_qty=Decimal("3"),
            avg_fill_price=Decimal("99.9"),
            fee=Decimal("0"),
        )
    )

    x10 = AsyncMock()
    x10.exchange = Exchange.X10

    market_data = MagicMock()
    market_data.get_orderbook.return_value = MagicMock(x10_bid=Decimal("99.9"), x10_ask=Decimal("100.1"))
    market_data.get_price.return_value = MagicMock(x10_price=Decimal("100.0"))
    market_data.get_market_info.return_value = MarketInfo(
        symbol="TEST",
        exchange=Exchange.X10,
        base_asset="TEST",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.0001"),
    )

    # Stay open through all IOC attempts, then be gone at verify (after market fallback).
    x10.get_position = AsyncMock(
        side_effect=[
            Position(
                symbol="TEST", exchange=Exchange.X10, side=trade.leg2.side, qty=Decimal("3"), entry_price=Decimal("100")
            ),
            Position(
                symbol="TEST", exchange=Exchange.X10, side=trade.leg2.side, qty=Decimal("3"), entry_price=Decimal("100")
            ),
            Position(
                symbol="TEST", exchange=Exchange.X10, side=trade.leg2.side, qty=Decimal("3"), entry_price=Decimal("100")
            ),
            Position(
                symbol="TEST", exchange=Exchange.X10, side=trade.leg2.side, qty=Decimal("3"), entry_price=Decimal("100")
            ),
            None,
        ]
    )

    placed_reqs: list[OrderRequest] = []

    async def _place_order(req: OrderRequest) -> Order:
        placed_reqs.append(req)
        oid = f"oid_{len(placed_reqs)}"
        return Order(
            order_id=oid,
            symbol=req.symbol,
            exchange=req.exchange,
            side=req.side,
            order_type=req.order_type,
            qty=req.qty,
            price=req.price,
            status=OrderStatus.PENDING,
        )

    x10.place_order = AsyncMock(side_effect=_place_order)

    async def _get_order(_symbol: str, order_id: str) -> Order | None:
        idx = int(order_id.split("_")[1])
        req = placed_reqs[idx - 1]
        if req.order_type == OrderType.MARKET:
            return Order(
                order_id=order_id,
                symbol=req.symbol,
                exchange=Exchange.X10,
                side=req.side,
                order_type=OrderType.MARKET,
                qty=req.qty,
                status=OrderStatus.FILLED,
                filled_qty=req.qty,
                avg_fill_price=Decimal("100.0"),
                fee=Decimal("0"),
            )
        return Order(
            order_id=order_id,
            symbol=req.symbol,
            exchange=Exchange.X10,
            side=req.side,
            order_type=req.order_type,
            qty=req.qty,
            price=req.price,
            status=OrderStatus.CANCELLED,
            filled_qty=Decimal("0"),
            avg_fill_price=Decimal("0"),
            fee=Decimal("0"),
        )

    x10.get_order = AsyncMock(side_effect=_get_order)

    store = AsyncMock()
    event_bus = AsyncMock()

    pm = PositionManager(settings, lighter, x10, store, event_bus, market_data)
    result = await pm.close_trade(trade, "EARLY_TAKE_PROFIT: synthetic slippage test")

    assert result.success is True
    assert trade.status == TradeStatus.CLOSED

    # With current settings, X10 smart close attempts a few IOC limits, then falls back to market.
    assert len(placed_reqs) >= int(settings.execution.x10_close_attempts) + 1
    assert placed_reqs[-1].order_type == OrderType.MARKET
    assert placed_reqs[-1].reduce_only is True


@pytest.mark.asyncio
async def test_current_settings_normal_close_path_runs_lighter_smart_then_x10_smart(monkeypatch):
    import funding_bot.services.positions as positions_mod

    monkeypatch.setattr(positions_mod.asyncio, "sleep", AsyncMock())

    settings = _load_current_settings()

    trade = Trade.create(
        symbol="TEST",
        leg1_exchange=Exchange.LIGHTER,
        leg1_side=Side.BUY,
        leg2_exchange=Exchange.X10,
        target_qty=Decimal("2"),
        target_notional_usd=Decimal("200"),
        entry_apy=Decimal("0.50"),
    )
    trade.status = TradeStatus.OPEN
    trade.opened_at = datetime.now(UTC) - timedelta(hours=49)
    trade.leg1.filled_qty = Decimal("2")
    trade.leg2.filled_qty = Decimal("2")
    trade.leg1.entry_price = Decimal("100")
    trade.leg2.entry_price = Decimal("100")

    lighter = AsyncMock()
    lighter.exchange = Exchange.LIGHTER
    x10 = AsyncMock()
    x10.exchange = Exchange.X10
    store = AsyncMock()
    event_bus = AsyncMock()
    market_data = MagicMock()

    pm = PositionManager(settings, lighter, x10, store, event_bus, market_data)

    calls: list[str] = []

    async def _close_lighter(_trade: Trade) -> None:
        calls.append("lighter")
        _trade.leg1.exit_price = Decimal("101")

    async def _close_x10(_trade: Trade) -> None:
        calls.append("x10")
        _trade.leg2.exit_price = Decimal("99")

    async def _verify(_trade: Trade) -> None:
        calls.append("verify")

    pm._close_lighter_smart = AsyncMock(side_effect=_close_lighter)  # type: ignore[method-assign]
    pm._close_x10_smart = AsyncMock(side_effect=_close_x10)  # type: ignore[method-assign]
    pm._verify_closed = AsyncMock(side_effect=_verify)  # type: ignore[method-assign]

    result = await pm.close_trade(trade, "Max hold time reached: synthetic test")

    assert result.success is True
    assert trade.status == TradeStatus.CLOSED
    assert calls[:2] == ["lighter", "x10"]
    assert "verify" in calls
