"""
Exchange Port: Abstract interface for exchange adapters.

All exchange-specific implementations (Lighter, X10) must implement this interface.
The interface uses only domain types - no SDK types leak through.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal

from funding_bot.domain.models import (
    Balance,
    Exchange,
    FundingRate,
    MarketInfo,
    Order,
    OrderRequest,
    Position,
)


class ExchangePort(ABC):
    """
    Abstract interface for exchange operations.

    All methods are async and use domain types.
    Implementations must map SDK-specific types to these domain types.
    """

    @property
    @abstractmethod
    def exchange(self) -> Exchange:
        """Return the exchange identifier."""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connection is healthy."""
        ...

    # =========================================================================
    # Lifecycle
    # =========================================================================

    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the adapter.

        Load markets, establish connections, etc.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """
        Close all connections and cleanup resources.
        """
        ...

    # =========================================================================
    # Market Data
    # =========================================================================

    @abstractmethod
    async def load_markets(self) -> dict[str, MarketInfo]:
        """
        Load all available markets.

        Returns a dict mapping symbol -> MarketInfo.
        """
        ...

    @abstractmethod
    async def get_market_info(self, symbol: str) -> MarketInfo | None:
        """Get market metadata for a symbol."""
        ...

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> Decimal:
        """
        Get current mark price for a symbol.

        Raises:
            DomainError if symbol not found or price unavailable.
        """
        ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """
        Get current funding rate for a symbol.

        Returns the rate for the current/next funding period.
        """
        ...

    @abstractmethod
    async def get_orderbook_l1(self, symbol: str) -> dict[str, Decimal]:
        """
        Get best bid/ask (Level 1 orderbook).

        Returns:
            {
                "best_bid": Decimal,
                "best_ask": Decimal,
                "bid_qty": Decimal,
                "ask_qty": Decimal,
                "timestamp": datetime,
            }
        """
        ...

    # =========================================================================
    # Account
    # =========================================================================

    @abstractmethod
    async def get_available_balance(self) -> Balance:
        """
        Get available margin/balance.

        Returns Balance object with available and total amounts.
        """
        ...

    @abstractmethod
    async def get_fee_schedule(self, symbol: str | None = None) -> dict[str, Decimal]:
        """
        Get fee schedule for the account.

        Returns:
            {
                "maker_fee": Decimal,
                "taker_fee": Decimal,
            }
        """
        ...

    # =========================================================================
    # Positions
    # =========================================================================

    @abstractmethod
    async def list_positions(self) -> list[Position]:
        """
        Get all open positions.

        Returns list of Position objects (only positions with qty != 0).
        """
        ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None:
        """
        Get position for a specific symbol.

        Returns None if no position exists.
        """
        ...

    @abstractmethod
    async def get_realized_funding(self, symbol: str, start_time: datetime | None = None) -> Decimal:
        """
        Get cumulative realized funding payment for a symbol since start_time.

        Args:
            symbol: Market symbol.
            start_time: Optional start time filter.

        Returns:
            Total funding amount (positive = received, negative = paid).
        """
        ...

    # =========================================================================
    # Orders
    # =========================================================================

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order:
        """
        Place an order.

        Args:
            request: OrderRequest with all order parameters.

        Returns:
            Order object with order_id and initial status.

        Raises:
            OrderRejectedError if order is rejected.
            InsufficientBalanceError if balance insufficient.
        """
        ...

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> Order | None:
        """
        Get order status by ID.

        Returns None if order not found.
        """
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        Cancel an open order.

        Returns:
            True if cancel succeeded or order already cancelled.
            False if order not found or already filled.
        """
        ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        """
        Cancel all open orders, optionally filtered by symbol.

        Returns number of orders cancelled.
        """
        ...

    async def modify_order(
        self,
        symbol: str,
        order_id: str,
        price: Decimal | None = None,
        qty: Decimal | None = None,
    ) -> bool:
        """
        Modify an existing order (price and/or quantity).

        Args:
            symbol: Market symbol.
            order_id: The order ID to modify.
            price: New price (optional).
            qty: New quantity (optional).

        Returns:
            True if modification succeeded (or request sent).
            False if not supported or failed.
        """
        return False

    # =========================================================================
    # WebSocket (optional - for implementations that support it)
    # =========================================================================

    async def subscribe_positions(self, callback: PositionCallback) -> None:
        """
        Subscribe to position updates.

        Default implementation does nothing (polling fallback).
        """
        return

    async def subscribe_orders(self, callback: OrderCallback) -> None:
        """
        Subscribe to order updates.

        Default implementation does nothing (polling fallback).
        """
        return

    async def subscribe_funding(self, callback: FundingCallback) -> None:
        """
        Subscribe to funding rate updates.

        Default implementation does nothing (polling fallback).
        """
        return

    async def subscribe_orderbook_l1(
        self,
        symbols: list[str] | None = None,
        *,
        depth: int | None = None,
    ) -> None:
        """
        Subscribe to public orderbook updates (best-effort).

        Implementations should update their internal orderbook/price caches so that
        MarketDataService can read near-real-time values without REST polling.

        Args:
            symbols: Optional symbol allowlist (exchange-native symbols). If None, subscribe to all.
            depth: Optional depth parameter if supported by the exchange stream.
        """
        return


# Type aliases for callbacks


PositionCallback = Callable[[Position], Awaitable[None]]
OrderCallback = Callable[[Order], Awaitable[None]]
FundingCallback = Callable[[FundingRate], Awaitable[None]]
