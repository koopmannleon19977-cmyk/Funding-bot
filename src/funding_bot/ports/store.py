"""
Trade Store Port: Abstract interface for trade persistence.

Handles storage and retrieval of trades and related data.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any

from funding_bot.domain.events import DomainEvent
from funding_bot.domain.historical import FundingCandle
from funding_bot.domain.models import ExecutionAttempt, Trade, TradeStatus


class TradeStorePort(ABC):
    """
    Abstract interface for trade storage.

    Implementations can be in-memory, SQLite, or any other storage.
    """

    # =========================================================================
    # Lifecycle
    # =========================================================================

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the store (create tables, etc)."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and cleanup."""
        ...

    # =========================================================================
    # Trade CRUD
    # =========================================================================

    @abstractmethod
    async def create_trade(self, trade: Trade) -> str:
        """
        Create a new trade.

        Returns the trade_id.
        """
        ...

    @abstractmethod
    async def get_trade(self, trade_id: str) -> Trade | None:
        """Get a trade by ID."""
        ...

    @abstractmethod
    async def get_trade_by_symbol(self, symbol: str) -> Trade | None:
        """
        Get the active trade for a symbol.

        Returns the most recent non-closed trade for the symbol.
        """
        ...

    @abstractmethod
    async def update_trade(self, trade_id: str, updates: dict[str, Any]) -> bool:
        """
        Update a trade with partial data.

        Args:
            trade_id: The trade to update.
            updates: Dict of field -> value to update.

        Returns:
            True if trade was found and updated.
        """
        ...

    @abstractmethod
    async def list_open_trades(self) -> list[Trade]:
        """Get all trades with status OPEN or OPENING."""
        ...

    @abstractmethod
    async def list_trades(
        self,
        status: TradeStatus | None = None,
        symbol: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Trade]:
        """
        List trades with optional filters.

        Args:
            status: Filter by status.
            symbol: Filter by symbol.
            since: Only trades created after this time.
            limit: Maximum number of trades to return.
        """
        ...

    # =========================================================================
    # Events (Audit Trail)
    # =========================================================================

    @abstractmethod
    async def append_event(self, trade_id: str, event: DomainEvent) -> None:
        """
        Append an event to the trade's audit log.

        Events are immutable and form an append-only log.
        """
        ...

    @abstractmethod
    async def list_events(
        self,
        trade_id: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]:
        """
        List events with optional filters.
        """
        ...

    # =========================================================================
    # Funding
    # =========================================================================

    @abstractmethod
    async def record_funding(
        self,
        trade_id: str,
        exchange: str,
        amount: Decimal,
        timestamp: datetime,
    ) -> None:
        """Record a funding payment for a trade."""
        ...

    @abstractmethod
    async def get_total_funding(self, trade_id: str) -> Decimal:
        """Get total funding collected for a trade."""
        ...

    async def get_funding_breakdown(self, trade_id: str) -> dict[str, Decimal]:
        """
        Get funding totals grouped by exchange label in `funding_events.exchange`.

        Returns e.g. {"LIGHTER": Decimal(...), "X10": Decimal(...)}.
        """
        raise NotImplementedError

    async def replace_funding_events(
        self,
        trade_id: str,
        events: list[dict[str, Any]],
    ) -> None:
        """
        Replace all funding_events rows for a trade with the provided list.

        Each event dict must include: exchange (str), amount (Decimal), timestamp (datetime).
        """
        raise NotImplementedError

    async def record_funding_history(
        self,
        symbol: str,
        exchange: str,
        rate_hourly: Decimal,
        rate_apy: Decimal | None = None,
        next_funding_time: datetime | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        """
        Record a funding rate snapshot for historical analysis.

        This stores raw funding rates from both exchanges for analytics:
        - APY stability detection
        - PnL attribution
        - Backtesting
        """
        raise NotImplementedError

    @abstractmethod
    async def get_recent_apy_history(
        self,
        symbol: str,
        hours_back: int = 168,
    ) -> list[Decimal]:
        """
        Get historical net APY series for velocity/Z-score exits.

        Returns list of hourly net APY values (oldest first, ASC chronological).
        Net APY = abs(lighter_apy - x10_apy) at each timestamp.
        Only timestamps with data from BOTH exchanges are included (intersection).

        Args:
            symbol: Trading symbol (e.g., "BTC-USD")
            hours_back: How many hours of history to return (default 168 = 7 days)

        Returns:
            List[Decimal]: Hourly net APY values, oldest first (most recent last)
            Empty list if insufficient data or one exchange missing
        """
        raise NotImplementedError

    # =========================================================================
    # PnL Snapshots
    # =========================================================================

    @abstractmethod
    async def save_pnl_snapshot(
        self,
        trade_id: str,
        realized_pnl: Decimal,
        unrealized_pnl: Decimal,
        funding: Decimal,
        fees: Decimal,
        timestamp: datetime,
    ) -> None:
        """Save a PnL snapshot for a trade."""
        ...

    # =========================================================================
    # Aggregations
    # =========================================================================

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """
        Get aggregate statistics.

        Returns:
            {
                "total_trades": int,
                "open_trades": int,
                "closed_trades": int,
                "total_pnl": Decimal,
                "total_funding": Decimal,
                "total_fees": Decimal,
                "win_rate": Decimal,
            }
        """
        ...

    # =========================================================================
    # Maintenance
    # =========================================================================

    @abstractmethod
    async def cleanup_old_data(self, days: int = 30) -> int:
        """
        Clean up old data (snapshots, events).

        Returns number of records deleted.
        """
        ...

    # =========================================================================
    # Execution Attempts / KPIs (Decision Log)
    # =========================================================================

    @abstractmethod
    async def create_execution_attempt(self, attempt: ExecutionAttempt) -> str:
        """Create a new execution attempt record."""
        ...

    @abstractmethod
    async def update_execution_attempt(self, attempt_id: str, updates: dict[str, Any]) -> bool:
        """Update an execution attempt with partial data."""
        ...

    @abstractmethod
    async def list_execution_attempts(
        self,
        symbol: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[ExecutionAttempt]:
        """List execution attempts with optional filters."""
        ...

    async def get_hedge_latency_stats(
        self,
        since: datetime | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """
        Get aggregated hedge latency statistics.

        Returns:
            {
                "count": int,
                "avg_submit_ms": Decimal,
                "avg_ack_ms": Decimal,
                "avg_place_ack_ms": Decimal,
                "min_submit_ms": Decimal,
                "max_submit_ms": Decimal,
                "by_symbol": {symbol: {"avg_submit_ms": ..., "count": ...}},
            }
        """
        raise NotImplementedError

    # =========================================================================
    # Historical Data (APY Analysis & Crash Detection)
    # =========================================================================

    async def insert_funding_candles(self, candles: list[FundingCandle]) -> int:
        """
        Batch insert minute-level funding candles with UPSERT to handle duplicates.

        Returns the number of records inserted.
        """
        raise NotImplementedError

    async def update_volatility_profile(
        self,
        symbol: str,
        profile: dict[str, Any],
    ) -> None:
        """Update volatility metrics for a symbol."""
        raise NotImplementedError

    async def get_volatility_profile(
        self,
        symbol: str,
        period_days: int = 90,
    ) -> dict[str, Any] | None:
        """
        Get the most recent volatility profile for a symbol.

        Returns dict with volatility metrics or None if not found.
        """
        raise NotImplementedError
