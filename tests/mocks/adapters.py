"""
Adapter Mocks for Testing.

Provides mock implementations of exchange adapters for safe testing
without hitting real APIs.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from funding_bot.domain.models import (
    Exchange,
    FundingRate,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
)


@dataclass
class MockMarket:
    """Simple market info for mocks."""
    symbol: str
    base_asset: str = "ETH"
    quote_asset: str = "USD"
    min_order_size: Decimal = Decimal("0.01")
    step_size: Decimal = Decimal("0.01")
    tick_size: Decimal = Decimal("0.01")
    max_leverage: Decimal = Decimal("20")


class MockLighterAdapter:
    """Mock Lighter adapter for testing."""

    def __init__(
        self,
        *,
        fill_orders: bool = True,
        fill_delay_ms: int = 0,
        fail_after_n_orders: int = 0,
    ):
        self.name = "LIGHTER"
        self.exchange = Exchange.LIGHTER
        self.fill_orders = fill_orders
        self.fill_delay_ms = fill_delay_ms
        self.fail_after_n_orders = fail_after_n_orders
        self._order_count = 0
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}

        # Markets
        self._markets = {
            "ETH": MockMarket(
                symbol="ETH",
                base_asset="ETH",
                quote_asset="USD",
                min_order_size=Decimal("0.01"),
                step_size=Decimal("0.01"),
                tick_size=Decimal("0.01"),
                max_leverage=Decimal("20"),
            ),
            "BTC": MockMarket(
                symbol="BTC",
                base_asset="BTC",
                quote_asset="USD",
                min_order_size=Decimal("0.0001"),
                step_size=Decimal("0.0001"),
                tick_size=Decimal("0.01"),
                max_leverage=Decimal("20"),
            ),
        }

    def get_MockMarket(self, symbol: str) -> MockMarket | None:
        """Get market info."""
        return self._markets.get(symbol)

    async def place_order(
        self,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: Decimal,
        price: Decimal | None = None,
        **kwargs: Any,
    ) -> Order:
        """Place a mock order."""
        self._order_count += 1

        # Simulate failure after N orders
        if self.fail_after_n_orders and self._order_count > self.fail_after_n_orders:
            raise Exception("Mock: Order failed after N orders")

        order_id = f"lighter-{self._order_count:04d}"

        status = OrderStatus.FILLED if self.fill_orders else OrderStatus.OPEN
        filled_qty = quantity if self.fill_orders else Decimal("0")

        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price or Decimal("2000"),
            quantity=quantity,
            filled_quantity=filled_qty,
            status=status,
            exchange=Exchange.LIGHTER,
            created_at=datetime.now(UTC),
        )

        self._orders[order_id] = order

        # Update position if filled
        if self.fill_orders:
            self._update_position(symbol, side, quantity, price or Decimal("2000"))

        return order

    async def get_order(self, symbol: str, order_id: str) -> Order | None:
        """Get order by ID."""
        return self._orders.get(order_id)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order."""
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            return True
        return False

    async def list_positions(self) -> list[Position]:
        """List all positions."""
        return list(self._positions.values())

    async def get_position(self, symbol: str) -> Position | None:
        """Get position for symbol."""
        return self._positions.get(symbol)

    async def close_position(self, symbol: str) -> Order | None:
        """Close a position."""
        if symbol in self._positions:
            pos = self._positions.pop(symbol)
            return await self.place_order(
                symbol=symbol,
                side=Side.BUY if pos.side == Side.SELL else Side.SELL,
                order_type=OrderType.MARKET,
                quantity=pos.qty,
            )
        return None

    async def get_funding_rate(self, symbol: str) -> FundingRate | None:
        """Get current funding rate."""
        return FundingRate(
            symbol=symbol,
            rate=Decimal("0.0005"),
            rate_hourly=Decimal("0.0005"),
            next_funding_time=datetime.now(UTC),
        )

    def _update_position(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal
    ) -> None:
        """Update internal position tracking."""
        if symbol in self._positions:
            pos = self._positions[symbol]
            if pos.side == side:
                # Adding to position
                new_qty = pos.qty + qty
                self._positions[symbol] = Position(
                    symbol=symbol,
                    exchange=Exchange.LIGHTER,
                    side=side,
                    qty=new_qty,
                    entry_price=(pos.entry_price * pos.qty + price * qty) / new_qty,
                )
            else:
                # Reducing position
                if qty >= pos.qty:
                    del self._positions[symbol]
                else:
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        exchange=Exchange.LIGHTER,
                        side=pos.side,
                        qty=pos.qty - qty,
                        entry_price=pos.entry_price,
                    )
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                exchange=Exchange.LIGHTER,
                side=side,
                qty=qty,
                entry_price=price,
            )


class MockX10Adapter:
    """Mock X10 adapter for testing."""

    def __init__(
        self,
        *,
        fill_orders: bool = True,
        fill_delay_ms: int = 0,
        fail_orders: bool = False,
    ):
        self.name = "X10"
        self.exchange = Exchange.X10
        self.fill_orders = fill_orders
        self.fill_delay_ms = fill_delay_ms
        self.fail_orders = fail_orders
        self._order_count = 0
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}

        # Markets (X10 uses -USD suffix)
        self._markets = {
            "ETH-USD": MockMarket(
                symbol="ETH-USD",
                base_asset="ETH",
                quote_asset="USD",
                min_order_size=Decimal("0.01"),
                step_size=Decimal("0.01"),
                tick_size=Decimal("0.01"),
                max_leverage=Decimal("20"),
            ),
            "BTC-USD": MockMarket(
                symbol="BTC-USD",
                base_asset="BTC",
                quote_asset="USD",
                min_order_size=Decimal("0.0001"),
                step_size=Decimal("0.0001"),
                tick_size=Decimal("0.01"),
                max_leverage=Decimal("20"),
            ),
        }

    def get_MockMarket(self, symbol: str) -> MockMarket | None:
        """Get market info."""
        # Handle both ETH and ETH-USD
        if "-USD" not in symbol:
            symbol = f"{symbol}-USD"
        return self._markets.get(symbol)

    async def place_order(
        self,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: Decimal,
        price: Decimal | None = None,
        **kwargs: Any,
    ) -> Order:
        """Place a mock order."""
        if self.fail_orders:
            raise Exception("Mock: X10 order failed")

        self._order_count += 1
        order_id = f"x10-{self._order_count:04d}"

        # Normalize symbol
        if "-USD" not in symbol:
            symbol = f"{symbol}-USD"

        status = OrderStatus.FILLED if self.fill_orders else OrderStatus.OPEN
        filled_qty = quantity if self.fill_orders else Decimal("0")

        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price or Decimal("2000"),
            quantity=quantity,
            filled_quantity=filled_qty,
            status=status,
            exchange=Exchange.X10,
            created_at=datetime.now(UTC),
        )

        self._orders[order_id] = order

        if self.fill_orders:
            self._update_position(symbol, side, quantity, price or Decimal("2000"))

        return order

    async def get_order(self, symbol: str, order_id: str) -> Order | None:
        """Get order by ID."""
        return self._orders.get(order_id)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order."""
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            return True
        return False

    async def list_positions(self) -> list[Position]:
        """List all positions."""
        return list(self._positions.values())

    async def get_position(self, symbol: str) -> Position | None:
        """Get position for symbol."""
        if "-USD" not in symbol:
            symbol = f"{symbol}-USD"
        return self._positions.get(symbol)

    async def close_position(self, symbol: str) -> Order | None:
        """Close a position."""
        if "-USD" not in symbol:
            symbol = f"{symbol}-USD"
        if symbol in self._positions:
            pos = self._positions.pop(symbol)
            return await self.place_order(
                symbol=symbol,
                side=Side.BUY if pos.side == Side.SELL else Side.SELL,
                order_type=OrderType.MARKET,
                quantity=pos.qty,
            )
        return None

    async def get_funding_rate(self, symbol: str) -> FundingRate | None:
        """Get current funding rate."""
        return FundingRate(
            symbol=symbol,
            rate=Decimal("-0.0002"),
            rate_hourly=Decimal("-0.0002"),
            next_funding_time=datetime.now(UTC),
        )

    def _update_position(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal
    ) -> None:
        """Update internal position tracking."""
        if symbol in self._positions:
            pos = self._positions[symbol]
            if pos.side == side:
                new_qty = pos.qty + qty
                self._positions[symbol] = Position(
                    symbol=symbol,
                    exchange=Exchange.X10,
                    side=side,
                    qty=new_qty,
                    entry_price=(pos.entry_price * pos.qty + price * qty) / new_qty,
                )
            else:
                if qty >= pos.qty:
                    del self._positions[symbol]
                else:
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        exchange=Exchange.X10,
                        side=pos.side,
                        qty=pos.qty - qty,
                        entry_price=pos.entry_price,
                    )
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                exchange=Exchange.X10,
                side=side,
                qty=qty,
                entry_price=price,
            )


class MockTradeStore:
    """Mock trade store for testing."""

    def __init__(self):
        self._trades: dict[str, dict] = {}
        self._trade_counter = 0

    async def create_trade(self, trade) -> str:
        """Create a new trade."""
        self._trade_counter += 1
        trade_id = f"trade-{self._trade_counter:04d}"
        self._trades[trade_id] = {
            "trade_id": trade_id,
            "symbol": trade.symbol,
            "status": trade.status.value,
            "trade": trade,
        }
        return trade_id

    async def update_trade(self, trade) -> bool:
        """Update trade."""
        if trade.trade_id in self._trades:
            self._trades[trade.trade_id]["trade"] = trade
            self._trades[trade.trade_id]["status"] = trade.status.value
            return True
        return False

    async def get_trade(self, trade_id: str):
        """Get trade by ID."""
        if trade_id in self._trades:
            return self._trades[trade_id]["trade"]
        return None

    async def list_open_trades(self):
        """List all open trades."""
        return [
            t["trade"] for t in self._trades.values() if t["status"] == "OPEN"
        ]


class MockEventBus:
    """Mock event bus for testing."""

    def __init__(self):
        self.events: list = []

    async def publish(self, event) -> None:
        """Publish event."""
        self.events.append(event)

    def get_events_of_type(self, event_type: type) -> list:
        """Get all events of a specific type."""
        return [e for e in self.events if isinstance(e, event_type)]

    def clear(self) -> None:
        """Clear all events."""
        self.events.clear()
