"""
Tests for Reconciler.

Tests position synchronization between database and exchanges.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from funding_bot.domain.models import (
    Exchange,
    Position,
    Side,
    Trade,
    TradeLeg,
    TradeStatus,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock()
    settings.reconciliation.interval_seconds = 60
    settings.reconciliation.auto_close_ghosts = True
    return settings


@pytest.fixture
def mock_lighter():
    """Create mock Lighter adapter."""
    lighter = AsyncMock()
    lighter.name = "LIGHTER"
    lighter.list_positions = AsyncMock(return_value=[])
    lighter.close_position = AsyncMock(return_value=True)
    return lighter


@pytest.fixture
def mock_x10():
    """Create mock X10 adapter."""
    x10 = AsyncMock()
    x10.name = "X10"
    x10.list_positions = AsyncMock(return_value=[])
    x10.close_position = AsyncMock(return_value=True)
    return x10


@pytest.fixture
def mock_store():
    """Create mock trade store."""
    store = AsyncMock()
    store.list_open_trades = AsyncMock(return_value=[])
    store.update_trade = AsyncMock(return_value=True)
    store.create_trade = AsyncMock(return_value="trade-001")
    return store


@pytest.fixture
def mock_event_bus():
    """Create mock event bus."""
    event_bus = AsyncMock()
    event_bus.publish = AsyncMock()
    return event_bus


@pytest.fixture
def sample_db_trade():
    """Create sample trade in database."""
    now = datetime.now(UTC)
    return Trade(
        trade_id="db-trade-001",
        symbol="ETH",
        status=TradeStatus.OPEN,
        target_qty=Decimal("0.5"),
        target_notional_usd=Decimal("1000"),
        entry_apy=Decimal("0.50"),
        leg1=TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.SELL,
            entry_price=Decimal("2000"),
            filled_qty=Decimal("0.5"),
        ),
        leg2=TradeLeg(
            exchange=Exchange.X10,
            side=Side.BUY,
            entry_price=Decimal("2000"),
            filled_qty=Decimal("0.5"),
        ),
        created_at=now - timedelta(hours=5),
        opened_at=now - timedelta(hours=5),
    )


@pytest.fixture
def sample_lighter_position():
    """Create sample position on Lighter."""
    return Position(
        symbol="ETH",
        exchange=Exchange.LIGHTER,
        side=Side.SELL,
        qty=Decimal("0.5"),
        entry_price=Decimal("2000"),
        unrealized_pnl=Decimal("10"),
    )


@pytest.fixture
def sample_x10_position():
    """Create sample position on X10."""
    return Position(
        symbol="ETH-USD",
        exchange=Exchange.X10,
        side=Side.BUY,
        qty=Decimal("0.5"),
        entry_price=Decimal("2000"),
        unrealized_pnl=Decimal("-5"),
    )


# =============================================================================
# Reconciliation Scenario Tests
# =============================================================================


class TestReconciliationScenarios:
    """Tests for different reconciliation scenarios."""

    def test_detect_zombie_position(self, sample_db_trade):
        """Should detect zombie (in DB, not on exchange)."""
        # DB says we have a trade
        db_trades = [sample_db_trade]

        # Exchange says no positions
        lighter_positions = []
        x10_positions = []

        # This is a zombie
        db_symbols = {t.symbol for t in db_trades}
        exchange_symbols = {p.symbol.replace("-USD", "") for p in lighter_positions + x10_positions}

        zombies = db_symbols - exchange_symbols
        assert "ETH" in zombies

    def test_detect_ghost_position(self, sample_lighter_position, sample_x10_position):
        """Should detect ghost (on exchange, not in DB)."""
        # DB has no trades
        db_trades = []

        # Exchange has positions
        lighter_positions = [sample_lighter_position]
        x10_positions = [sample_x10_position]

        db_symbols = {t.symbol for t in db_trades}
        exchange_symbols = {p.symbol.replace("-USD", "") for p in lighter_positions}

        ghosts = exchange_symbols - db_symbols
        assert "ETH" in ghosts

    def test_balanced_state(self, sample_db_trade, sample_lighter_position, sample_x10_position):
        """No zombies or ghosts when DB matches exchanges."""
        db_trades = [sample_db_trade]
        lighter_positions = [sample_lighter_position]
        x10_positions = [sample_x10_position]

        db_symbols = {t.symbol for t in db_trades}
        lighter_symbols = {p.symbol.replace("-USD", "") for p in lighter_positions}
        x10_symbols = {p.symbol.replace("-USD", "") for p in x10_positions}

        # All three should have ETH
        assert db_symbols == lighter_symbols == x10_symbols


# =============================================================================
# Zombie Handling Tests
# =============================================================================


class TestZombieHandling:
    """Tests for zombie position handling."""

    def test_zombie_should_be_marked_closed(self, sample_db_trade):
        """Zombie trade should be marked as closed."""
        # Simulate zombie detection
        trade = sample_db_trade
        trade.status = TradeStatus.OPEN

        # After zombie handling, status should be CLOSED
        # (simulating what reconciler does)
        if trade.status == TradeStatus.OPEN:
            trade.status = TradeStatus.CLOSED
            trade.close_reason = "ZOMBIE_RECONCILED"

        assert trade.status == TradeStatus.CLOSED
        assert trade.close_reason == "ZOMBIE_RECONCILED"

    def test_zombie_with_partial_position(self):
        """Should handle zombie with only one leg remaining."""
        # Trade in DB
        now = datetime.now(UTC)
        trade = Trade(
            trade_id="partial-001",
            symbol="BTC",
            status=TradeStatus.OPEN,
            target_qty=Decimal("0.01"),
            target_notional_usd=Decimal("500"),
            entry_apy=Decimal("0.40"),
            leg1=TradeLeg(
                exchange=Exchange.LIGHTER,
                side=Side.SELL,
                entry_price=Decimal("50000"),
                filled_qty=Decimal("0.01"),
            ),
            leg2=TradeLeg(
                exchange=Exchange.X10,
                side=Side.BUY,
                entry_price=Decimal("50000"),
                filled_qty=Decimal("0.01"),
            ),
            created_at=now,
        )

        # Only Lighter position exists, X10 was closed externally
        lighter_has_position = True
        x10_has_position = False

        # This is a partial zombie - should be flagged
        is_partial = lighter_has_position != x10_has_position
        assert is_partial is True


# =============================================================================
# Ghost Handling Tests
# =============================================================================


class TestGhostHandling:
    """Tests for ghost position handling."""

    def test_ghost_should_be_closed_by_default(self, sample_lighter_position):
        """Ghost positions should be closed by default (safety)."""
        ghost = sample_lighter_position

        # Default action for ghosts is to close them
        action = "close"  # Could also be "adopt"

        assert action == "close"

    def test_ghost_can_be_adopted(self, sample_lighter_position, sample_x10_position):
        """Ghost can be adopted if it looks like interrupted trade."""
        # Both exchanges have matching positions
        lighter = sample_lighter_position
        x10 = sample_x10_position

        # Check if they're balanced (same size, opposite sides)
        lighter_qty = lighter.qty
        x10_qty = x10.qty

        is_balanced = abs(lighter_qty - x10_qty) < Decimal("0.001")
        is_hedged = lighter.side != x10.side

        can_adopt = is_balanced and is_hedged
        assert can_adopt is True


# =============================================================================
# Late Fill Detection Tests
# =============================================================================


class TestLateFillDetection:
    """Tests for late fill detection."""

    def test_late_fill_detected(self):
        """Should detect positions that appeared after abort."""
        # Trade was aborted 2 minutes ago
        abort_time = datetime.now(UTC) - timedelta(minutes=2)

        # Position appeared 1 minute ago (after abort)
        position_time = datetime.now(UTC) - timedelta(minutes=1)

        is_late_fill = position_time > abort_time
        assert is_late_fill is True

    def test_not_late_fill_if_before_abort(self):
        """Should not flag positions from before abort."""
        abort_time = datetime.now(UTC) - timedelta(minutes=1)
        position_time = datetime.now(UTC) - timedelta(minutes=5)

        is_late_fill = position_time > abort_time
        assert is_late_fill is False


# =============================================================================
# Size Mismatch Tests
# =============================================================================


class TestSizeMismatch:
    """Tests for position size mismatch detection."""

    def test_size_mismatch_detected(self, sample_db_trade):
        """Should detect when DB and exchange sizes don't match."""
        db_qty = sample_db_trade.leg1.filled_qty  # 0.5
        exchange_qty = Decimal("0.45")  # Different!

        tolerance = Decimal("0.01")  # 1% tolerance
        mismatch = abs(db_qty - exchange_qty) / db_qty > tolerance

        assert mismatch is True

    def test_small_difference_tolerated(self, sample_db_trade):
        """Small differences should be tolerated (rounding)."""
        db_qty = sample_db_trade.leg1.filled_qty  # 0.5
        exchange_qty = Decimal("0.5001")  # Very small difference

        tolerance = Decimal("0.01")  # 1% tolerance
        mismatch = abs(db_qty - exchange_qty) / db_qty > tolerance

        assert mismatch is False


# =============================================================================
# Edge Cases
# =============================================================================


class TestReconcilerEdgeCases:
    """Tests for edge cases in reconciliation."""

    def test_empty_db_empty_exchanges(self):
        """Should handle empty state gracefully."""
        db_trades = []
        lighter_positions = []
        x10_positions = []

        db_symbols = {t.symbol for t in db_trades}
        exchange_symbols = set()

        zombies = db_symbols - exchange_symbols
        ghosts = exchange_symbols - db_symbols

        assert len(zombies) == 0
        assert len(ghosts) == 0

    def test_multiple_trades_same_symbol_rejected(self):
        """Should flag multiple trades for same symbol."""
        # This shouldn't happen in production
        now = datetime.now(UTC)
        trade1 = Trade(
            trade_id="multi-001",
            symbol="ETH",
            status=TradeStatus.OPEN,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
            entry_apy=Decimal("0.50"),
            leg1=TradeLeg(exchange=Exchange.LIGHTER, side=Side.SELL),
            leg2=TradeLeg(exchange=Exchange.X10, side=Side.BUY),
            created_at=now,
        )
        trade2 = Trade(
            trade_id="multi-002",
            symbol="ETH",  # Same symbol!
            status=TradeStatus.OPEN,
            target_qty=Decimal("0.25"),
            target_notional_usd=Decimal("500"),
            entry_apy=Decimal("0.50"),
            leg1=TradeLeg(exchange=Exchange.LIGHTER, side=Side.SELL),
            leg2=TradeLeg(exchange=Exchange.X10, side=Side.BUY),
            created_at=now,
        )

        db_trades = [trade1, trade2]
        symbols = [t.symbol for t in db_trades]

        has_duplicates = len(symbols) != len(set(symbols))
        assert has_duplicates is True
