"""
Tests for surge pro risk-related store queries.

These tests verify that the RiskGuard query methods correctly:
- Calculate daily/hourly PnL from closed trades
- Retrieve recent trades for loss streak detection
- Calculate fill rates for market quality assessment
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from funding_bot.adapters.store.sqlite.store import SQLiteTradeStore
from funding_bot.config.settings import Settings
from funding_bot.domain.models import Exchange, Side, SurgeTrade, SurgeTradeStatus


@pytest.fixture
async def sqlite_store():
    """Create an empty in-memory store for testing."""
    settings = Settings()
    settings.database.path = ":memory:"

    store = SQLiteTradeStore(settings)
    await store.initialize()
    yield store
    await store.close()


@pytest.mark.asyncio
class TestGetDailyPnl:
    """Tests for daily PnL calculation."""

    async def test_get_daily_pnl_empty(self, sqlite_store):
        """Should return 0 when no trades exist."""
        daily_pnl = await sqlite_store.get_daily_pnl()
        assert daily_pnl == Decimal("0")

    async def test_get_daily_pnl_calculates_correctly(self, sqlite_store):
        """Should calculate daily PnL from closed trades."""
        now = datetime.now(UTC)

        # Create winning and losing trades
        trades_data = [
            # BUY: entry=100, exit=101, qty=1 -> profit = (101-100)*1 = 1
            ("daily_0", Side.BUY, "100", "101", "1.0", "0"),
            # BUY: entry=100, exit=99, qty=1 -> loss = (99-100)*1 = -1
            ("daily_1", Side.BUY, "100", "99", "1.0", "0"),
            # BUY: entry=100, exit=100.5, qty=1 -> profit = (100.5-100)*1 = 0.5
            ("daily_2", Side.BUY, "100", "100.5", "1.0", "0"),
        ]

        for trade_id, side, entry, exit_price, qty, fees in trades_data:
            trade = SurgeTrade(
                trade_id=trade_id,
                symbol="BTC",
                exchange=Exchange.X10,
                side=side,
                qty=Decimal(qty),
                entry_price=Decimal(entry),
                exit_price=Decimal(exit_price),
                fees=Decimal(fees),
                status=SurgeTradeStatus.CLOSED,
                opened_at=now - timedelta(hours=1),
                closed_at=now,
            )
            await sqlite_store.create_surge_trade(trade)

        daily_pnl = await sqlite_store.get_daily_pnl()
        # +1 - 1 + 0.5 = 0.5
        assert daily_pnl == Decimal("0.5")

    async def test_get_daily_pnl_respects_side(self, sqlite_store):
        """Should calculate PnL correctly for SELL side."""
        now = datetime.now(UTC)

        # SELL: entry=100, exit=99 -> profit = (100-99)*1 = 1 (price went down)
        trade = SurgeTrade(
            trade_id="sell_trade",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.SELL,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            exit_price=Decimal("99"),
            fees=Decimal("0"),
            status=SurgeTradeStatus.CLOSED,
            opened_at=now - timedelta(hours=1),
            closed_at=now,
        )
        await sqlite_store.create_surge_trade(trade)

        daily_pnl = await sqlite_store.get_daily_pnl()
        assert daily_pnl == Decimal("1")

    async def test_get_daily_pnl_subtracts_fees(self, sqlite_store):
        """Should subtract fees from PnL."""
        now = datetime.now(UTC)

        trade = SurgeTrade(
            trade_id="fee_trade",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            exit_price=Decimal("102"),
            fees=Decimal("0.5"),  # $0.50 fee
            status=SurgeTradeStatus.CLOSED,
            opened_at=now - timedelta(hours=1),
            closed_at=now,
        )
        await sqlite_store.create_surge_trade(trade)

        daily_pnl = await sqlite_store.get_daily_pnl()
        # (102-100)*1 - 0.5 = 1.5
        assert daily_pnl == Decimal("1.5")

    async def test_get_daily_pnl_excludes_old_trades(self, sqlite_store):
        """Should exclude trades closed before today."""
        now = datetime.now(UTC)
        yesterday = now - timedelta(days=1)

        # Trade closed yesterday - should be excluded
        old_trade = SurgeTrade(
            trade_id="old_trade",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            fees=Decimal("0"),
            status=SurgeTradeStatus.CLOSED,
            opened_at=yesterday - timedelta(hours=1),
            closed_at=yesterday,
        )
        await sqlite_store.create_surge_trade(old_trade)

        # Trade closed today - should be included
        today_trade = SurgeTrade(
            trade_id="today_trade",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            fees=Decimal("0"),
            status=SurgeTradeStatus.CLOSED,
            opened_at=now - timedelta(hours=1),
            closed_at=now,
        )
        await sqlite_store.create_surge_trade(today_trade)

        daily_pnl = await sqlite_store.get_daily_pnl()
        # Only today's trade: +1
        assert daily_pnl == Decimal("1")


@pytest.mark.asyncio
class TestGetHourlyPnl:
    """Tests for hourly PnL calculation."""

    async def test_get_hourly_pnl_empty(self, sqlite_store):
        """Should return 0 when no trades exist."""
        hourly_pnl = await sqlite_store.get_hourly_pnl()
        assert hourly_pnl == Decimal("0")

    async def test_get_hourly_pnl_calculates_correctly(self, sqlite_store):
        """Should calculate hourly PnL from recently closed trades."""
        now = datetime.now(UTC)

        trade = SurgeTrade(
            trade_id="hourly_trade",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("2.0"),
            entry_price=Decimal("100"),
            exit_price=Decimal("105"),
            fees=Decimal("1"),
            status=SurgeTradeStatus.CLOSED,
            opened_at=now - timedelta(minutes=30),
            closed_at=now - timedelta(minutes=10),
        )
        await sqlite_store.create_surge_trade(trade)

        hourly_pnl = await sqlite_store.get_hourly_pnl()
        # (105-100)*2 - 1 = 9
        assert hourly_pnl == Decimal("9")

    async def test_get_hourly_pnl_excludes_old_trades(self, sqlite_store):
        """Should exclude trades closed more than 1 hour ago."""
        now = datetime.now(UTC)

        # Trade closed 2 hours ago - should be excluded
        old_trade = SurgeTrade(
            trade_id="old_hourly",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            exit_price=Decimal("200"),  # Big profit
            fees=Decimal("0"),
            status=SurgeTradeStatus.CLOSED,
            opened_at=now - timedelta(hours=3),
            closed_at=now - timedelta(hours=2),
        )
        await sqlite_store.create_surge_trade(old_trade)

        hourly_pnl = await sqlite_store.get_hourly_pnl()
        assert hourly_pnl == Decimal("0")


@pytest.mark.asyncio
class TestGetRecentTrades:
    """Tests for recent trades retrieval."""

    async def test_get_recent_trades_empty(self, sqlite_store):
        """Should return empty list when no trades exist."""
        recent = await sqlite_store.get_recent_trades(count=10)
        assert recent == []

    async def test_get_recent_trades_returns_correct_count(self, sqlite_store):
        """Should return N most recent trades."""
        now = datetime.now(UTC)

        for i in range(5):
            trade = SurgeTrade(
                trade_id=f"recent_{i}",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.BUY,
                qty=Decimal("1.0"),
                entry_price=Decimal("100"),
                exit_price=Decimal("101"),
                fees=Decimal("0"),
                status=SurgeTradeStatus.CLOSED,
                opened_at=now - timedelta(hours=5 - i),
                closed_at=now - timedelta(hours=5 - i) + timedelta(minutes=30),
            )
            await sqlite_store.create_surge_trade(trade)

        recent = await sqlite_store.get_recent_trades(count=3)
        assert len(recent) == 3

    async def test_get_recent_trades_ordered_by_closed_at_desc(self, sqlite_store):
        """Should return trades ordered by closed_at descending."""
        now = datetime.now(UTC)

        # Create trades with different close times
        for i in range(3):
            trade = SurgeTrade(
                trade_id=f"ordered_{i}",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.BUY,
                qty=Decimal("1.0"),
                entry_price=Decimal("100"),
                exit_price=Decimal("101"),
                fees=Decimal("0"),
                status=SurgeTradeStatus.CLOSED,
                opened_at=now - timedelta(hours=3 - i),
                closed_at=now - timedelta(hours=3 - i) + timedelta(minutes=30),
            )
            await sqlite_store.create_surge_trade(trade)

        recent = await sqlite_store.get_recent_trades(count=3)
        # Most recent (ordered_2) should be first
        assert recent[0].trade_id == "ordered_2"
        assert recent[1].trade_id == "ordered_1"
        assert recent[2].trade_id == "ordered_0"

    async def test_get_recent_trades_excludes_open_trades(self, sqlite_store):
        """Should only return CLOSED trades."""
        now = datetime.now(UTC)

        # Closed trade
        closed_trade = SurgeTrade(
            trade_id="closed_one",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            fees=Decimal("0"),
            status=SurgeTradeStatus.CLOSED,
            opened_at=now - timedelta(hours=1),
            closed_at=now,
        )
        await sqlite_store.create_surge_trade(closed_trade)

        # Open trade - should be excluded
        open_trade = SurgeTrade(
            trade_id="open_one",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            fees=Decimal("0"),
            status=SurgeTradeStatus.OPEN,
            opened_at=now - timedelta(minutes=30),
        )
        await sqlite_store.create_surge_trade(open_trade)

        recent = await sqlite_store.get_recent_trades(count=10)
        assert len(recent) == 1
        assert recent[0].trade_id == "closed_one"


@pytest.mark.asyncio
class TestGetHourlyFillRate:
    """Tests for hourly fill rate calculation."""

    async def test_get_hourly_fill_rate_empty(self, sqlite_store):
        """Should return 0 when no trades exist."""
        fill_rate = await sqlite_store.get_hourly_fill_rate()
        assert fill_rate == 0.0

    async def test_get_hourly_fill_rate_all_filled(self, sqlite_store):
        """Should return 1.0 when all trades are filled."""
        now = datetime.now(UTC)

        for i in range(3):
            trade = SurgeTrade(
                trade_id=f"filled_{i}",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.BUY,
                qty=Decimal("1.0"),
                entry_price=Decimal("100"),
                exit_price=Decimal("101"),
                fees=Decimal("0"),
                status=SurgeTradeStatus.CLOSED,
                opened_at=now - timedelta(minutes=30),
                closed_at=now - timedelta(minutes=10),
            )
            await sqlite_store.create_surge_trade(trade)

        fill_rate = await sqlite_store.get_hourly_fill_rate()
        assert fill_rate == 1.0

    async def test_get_hourly_fill_rate_partial(self, sqlite_store):
        """Should calculate fill rate correctly with failed trades."""
        now = datetime.now(UTC)

        # 2 filled trades
        for i in range(2):
            trade = SurgeTrade(
                trade_id=f"partial_filled_{i}",
                symbol="BTC",
                exchange=Exchange.X10,
                side=Side.BUY,
                qty=Decimal("1.0"),
                entry_price=Decimal("100"),
                exit_price=Decimal("101"),
                fees=Decimal("0"),
                status=SurgeTradeStatus.CLOSED,
                opened_at=now - timedelta(minutes=30),
                closed_at=now - timedelta(minutes=10),
            )
            await sqlite_store.create_surge_trade(trade)

        # 1 failed trade
        failed_trade = SurgeTrade(
            trade_id="partial_failed",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("0"),
            fees=Decimal("0"),
            status=SurgeTradeStatus.FAILED,
            opened_at=now - timedelta(minutes=20),
        )
        await sqlite_store.create_surge_trade(failed_trade)

        fill_rate = await sqlite_store.get_hourly_fill_rate()
        # 2 filled / 3 total = 0.666...
        assert abs(fill_rate - (2 / 3)) < 0.01

    async def test_get_hourly_fill_rate_excludes_old_trades(self, sqlite_store):
        """Should exclude trades opened more than 1 hour ago."""
        now = datetime.now(UTC)

        # Old trade (opened 2 hours ago)
        old_trade = SurgeTrade(
            trade_id="old_fill",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            fees=Decimal("0"),
            status=SurgeTradeStatus.CLOSED,
            opened_at=now - timedelta(hours=2),
            closed_at=now - timedelta(hours=2) + timedelta(minutes=10),
        )
        await sqlite_store.create_surge_trade(old_trade)

        fill_rate = await sqlite_store.get_hourly_fill_rate()
        # Old trade excluded, so no trades in window
        assert fill_rate == 0.0


@pytest.mark.asyncio
class TestSurgeTradesCRUD:
    """Tests for basic surge trade CRUD operations."""

    async def test_create_and_get_surge_trade(self, sqlite_store):
        """Should create and retrieve a surge trade."""
        now = datetime.now(UTC)

        trade = SurgeTrade(
            trade_id="crud_test",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.SELL,
            qty=Decimal("0.5"),
            entry_price=Decimal("2000"),
            fees=Decimal("0.1"),
            status=SurgeTradeStatus.OPEN,
            entry_order_id="order_123",
            entry_reason="test_signal",
            opened_at=now,
        )
        await sqlite_store.create_surge_trade(trade)

        retrieved = await sqlite_store.get_surge_trade("crud_test")
        assert retrieved is not None
        assert retrieved.trade_id == "crud_test"
        assert retrieved.symbol == "ETH"
        assert retrieved.exchange == Exchange.X10
        assert retrieved.side == Side.SELL
        assert retrieved.qty == Decimal("0.5")
        assert retrieved.entry_price == Decimal("2000")
        assert retrieved.status == SurgeTradeStatus.OPEN
        assert retrieved.entry_order_id == "order_123"

    async def test_update_surge_trade(self, sqlite_store):
        """Should update a surge trade."""
        now = datetime.now(UTC)

        trade = SurgeTrade(
            trade_id="update_test",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            entry_price=Decimal("100"),
            status=SurgeTradeStatus.OPEN,
            opened_at=now,
        )
        await sqlite_store.create_surge_trade(trade)

        # Update the trade
        trade.exit_price = Decimal("105")
        trade.fees = Decimal("0.5")
        trade.status = SurgeTradeStatus.CLOSED
        trade.closed_at = now + timedelta(minutes=30)
        trade.exit_reason = "take_profit"

        await sqlite_store.update_surge_trade(trade)

        retrieved = await sqlite_store.get_surge_trade("update_test")
        assert retrieved.exit_price == Decimal("105")
        assert retrieved.fees == Decimal("0.5")
        assert retrieved.status == SurgeTradeStatus.CLOSED
        assert retrieved.exit_reason == "take_profit"

    async def test_list_open_surge_trades(self, sqlite_store):
        """Should list only open/pending surge trades."""
        now = datetime.now(UTC)

        # Open trade
        open_trade = SurgeTrade(
            trade_id="open_list",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("1.0"),
            status=SurgeTradeStatus.OPEN,
            opened_at=now,
        )
        await sqlite_store.create_surge_trade(open_trade)

        # Pending trade
        pending_trade = SurgeTrade(
            trade_id="pending_list",
            symbol="ETH",
            exchange=Exchange.X10,
            side=Side.SELL,
            qty=Decimal("0.5"),
            status=SurgeTradeStatus.PENDING,
        )
        await sqlite_store.create_surge_trade(pending_trade)

        # Closed trade
        closed_trade = SurgeTrade(
            trade_id="closed_list",
            symbol="SOL",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("10"),
            entry_price=Decimal("20"),
            exit_price=Decimal("21"),
            status=SurgeTradeStatus.CLOSED,
            opened_at=now - timedelta(hours=1),
            closed_at=now,
        )
        await sqlite_store.create_surge_trade(closed_trade)

        open_trades = await sqlite_store.list_open_surge_trades()
        assert len(open_trades) == 2
        trade_ids = {t.trade_id for t in open_trades}
        assert "open_list" in trade_ids
        assert "pending_list" in trade_ids
        assert "closed_list" not in trade_ids
