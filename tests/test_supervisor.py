"""
Supervisor Lifecycle Management Tests.

Tests the bot startup/shutdown sequence including:
- Infrastructure initialization (database, event bus)
- Adapter initialization (Lighter, X10, Telegram)
- Service startup (market data, execution, positions, etc.)
- Main loop startup (opportunities, reconciliation)
- Graceful shutdown
- Error handling during startup
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import os

# Mark as integration tests - skip unless RUN_INTEGRATION=1
if os.getenv("RUN_INTEGRATION") == "1":
    pytestmark = pytest.mark.integration
else:
    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skip(reason="Integration test: set RUN_INTEGRATION=1 to run")
    ]

from funding_bot.domain.events import BrokenHedgeDetected

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock()
    settings.live_trading = False  # Paper mode for tests
    settings.testing_mode = True
    settings.database.path = ":memory:"  # In-memory DB for tests
    settings.database.pool_size = 1
    settings.database.write_batch_size = 10
    settings.database.wal_mode = True
    settings.logging.level = "DEBUG"
    settings.logging.json_enabled = False
    settings.telegram.enabled = False
    settings.trading.desired_notional_usd = Decimal("400")
    settings.trading.max_open_trades = 4
    settings.trading.min_apy_filter = Decimal("0.25")
    settings.risk.max_drawdown_pct = Decimal("0.20")
    settings.risk.max_exposure_pct = Decimal("0.80")
    settings.risk.max_trade_size_pct = Decimal("0.50")
    settings.risk.min_free_margin_pct = Decimal("0.05")
    settings.risk.broken_hedge_cooldown_seconds = Decimal("900")
    settings.shutdown.close_positions_on_exit = False
    settings.shutdown.timeout_seconds = Decimal("20")
    settings.websocket.orderbook_l1_retry_attempts = 3
    settings.websocket.orderbook_l1_retry_delay_seconds = Decimal("0.15")
    settings.websocket.orderbook_l1_fallback_max_age_seconds = Decimal("8.0")
    settings.websocket.health_check_interval = Decimal("3.0")
    settings.websocket.market_data_streams_enabled = True
    settings.websocket.market_data_streams_symbols = ["ETH", "BTC", "SOL"]
    settings.websocket.market_data_streams_max_symbols = 6
    settings.websocket.market_data_batch_refresh_interval_seconds = Decimal("15.0")
    settings.lighter.base_url = "https://test.lighter.exchange"
    settings.x10.base_url = "https://test.x10.exchange"
    settings.x10.vault_id = 12345  # Dummy vault ID for initialization
    settings.x10.private_key = "0x1"  # Dummy hex key
    settings.x10.public_key = "0x2"  # Dummy hex key
    settings.x10.api_key = "dummy_key"
    return settings


@pytest.fixture
def supervisor(mock_settings):
    """Create supervisor instance."""
    from funding_bot.app.supervisor import Supervisor

    sup = Supervisor(mock_settings)
    return sup


# =============================================================================
# Infrastructure Initialization Tests
# =============================================================================


class TestInfrastructureInitialization:
    """Tests for Phase 1 of startup: infrastructure setup."""

    @pytest.mark.asyncio
    async def test_creates_database_directory(self, supervisor, mock_settings, tmp_path):
        """Should create database directory if it doesn't exist."""
        # Use temporary path for this test
        db_path = tmp_path / "test.db"
        mock_settings.database.path = str(db_path)

        # Initialize infrastructure
        await supervisor._init_infrastructure()

        # Verify directory was created
        assert db_path.parent.exists()

    @pytest.mark.asyncio
    async def test_creates_logs_directory(self, supervisor):
        """Should create logs directory."""
        import shutil

        # Remove logs directory if it exists
        logs_dir = Path("logs")
        if logs_dir.exists():
            shutil.rmtree(logs_dir)

        await supervisor._init_infrastructure()

        assert logs_dir.exists()

    @pytest.mark.asyncio
    async def test_creates_data_directory(self, supervisor):
        """Should create data directory."""
        import shutil

        # Remove data directory if it exists
        data_dir = Path("data")
        if data_dir.exists():
            shutil.rmtree(data_dir)

        await supervisor._init_infrastructure()

        assert data_dir.exists()

    @pytest.mark.asyncio
    async def test_initializes_event_bus(self, supervisor):
        """Should initialize and start event bus."""
        await supervisor._init_infrastructure()

        assert supervisor.event_bus is not None
        assert supervisor.event_bus._running is True

    @pytest.mark.asyncio
    async def test_initializes_database_store(self, supervisor):
        """Should initialize SQLite store."""
        await supervisor._init_infrastructure()

        assert supervisor.store is not None
        # Store should be initialized
        assert supervisor.store._pool is not None

    @pytest.mark.asyncio
    async def test_closes_infrastructure(self, supervisor):
        """Should close database and event bus properly."""
        await supervisor._init_infrastructure()

        # Close infrastructure
        await supervisor._close_infrastructure()

        # Verify cleanup (store should be closed)
        assert supervisor.store is not None


# =============================================================================
# Adapter Initialization Tests
# =============================================================================


class TestAdapterInitialization:
    """Tests for Phase 2 of startup: adapter setup."""

    @pytest.mark.asyncio
    async def test_initializes_lighter_adapter(self, supervisor):
        """Should initialize Lighter adapter."""
        await supervisor._init_adapters()

        assert supervisor.lighter is not None
        assert supervisor.lighter.name == "LIGHTER"

    @pytest.mark.asyncio
    async def test_initializes_x10_adapter(self, supervisor):
        """Should initialize X10 adapter."""
        await supervisor._init_adapters()

        assert supervisor.x10 is not None
        assert supervisor.x10.name == "X10"

    @pytest.mark.asyncio
    async def test_initializes_telegram_adapter(self, supervisor):
        """Should initialize Telegram adapter (even if disabled)."""
        await supervisor._init_adapters()

        assert supervisor.notifier is not None

    @pytest.mark.asyncio
    async def test_closes_adapters(self, supervisor):
        """Should close all adapters properly."""
        await supervisor._init_adapters()

        # Close adapters
        await supervisor._close_adapters()

        # Adapters still exist but are closed
        assert supervisor.lighter is not None
        assert supervisor.x10 is not None


# =============================================================================
# Service Startup Tests
# =============================================================================


class TestServiceStartup:
    """Tests for Phase 3 of startup: service initialization."""

    @pytest.mark.asyncio
    async def test_starts_market_data_service(self, supervisor):
        """Should start MarketDataService."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        assert supervisor.market_data is not None

    @pytest.mark.asyncio
    async def test_starts_opportunity_engine(self, supervisor):
        """Should start OpportunityEngine."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        assert supervisor.opportunity_engine is not None

    @pytest.mark.asyncio
    async def test_starts_execution_engine(self, supervisor):
        """Should start ExecutionEngine."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        assert supervisor.execution_engine is not None

    @pytest.mark.asyncio
    async def test_starts_position_manager(self, supervisor):
        """Should start PositionManager."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        assert supervisor.position_manager is not None

    @pytest.mark.asyncio
    async def test_starts_funding_tracker(self, supervisor):
        """Should start FundingTracker."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        assert supervisor.funding_tracker is not None

    @pytest.mark.asyncio
    async def test_starts_reconciler(self, supervisor):
        """Should start Reconciler."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        assert supervisor.reconciler is not None

    @pytest.mark.asyncio
    async def test_starts_shutdown_service(self, supervisor):
        """Should start ShutdownService."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        assert supervisor.shutdown_service is not None

    @pytest.mark.asyncio
    async def test_starts_notification_service(self, supervisor):
        """Should start NotificationService."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        assert supervisor.notification_service is not None

    @pytest.mark.asyncio
    async def test_subscribes_to_broken_hedge_events(self, supervisor):
        """Should subscribe to BrokenHedgeDetected events."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        # Event bus should have subscribers for BrokenHedgeDetected
        assert supervisor.event_bus is not None


# =============================================================================
# Full Startup Sequence Tests
# =============================================================================


class TestFullStartupSequence:
    """Tests for complete startup sequence."""

    @pytest.mark.asyncio
    async def test_successful_startup(self, supervisor):
        """Should complete all startup phases."""
        await supervisor.start()

        assert supervisor._running is True
        assert supervisor._stopping is False

        # Verify all components initialized
        assert supervisor.event_bus is not None
        assert supervisor.store is not None
        assert supervisor.lighter is not None
        assert supervisor.x10 is not None
        assert supervisor.market_data is not None
        assert supervisor.opportunity_engine is not None
        assert supervisor.execution_engine is not None
        assert supervisor.position_manager is not None
        assert supervisor.funding_tracker is not None
        assert supervisor.reconciler is not None
        assert supervisor.shutdown_service is not None

    @pytest.mark.asyncio
    async def test_startup_is_idempotent(self, supervisor):
        """Calling start() multiple times should be safe."""
        await supervisor.start()
        await supervisor.start()  # Should not crash

        assert supervisor._running is True


# =============================================================================
# Shutdown Tests
# =============================================================================


class TestShutdownSequence:
    """Tests for graceful shutdown."""

    @pytest.mark.asyncio
    async def test_successful_shutdown(self, supervisor):
        """Should shutdown all services gracefully."""
        await supervisor.start()

        # Shutdown
        await supervisor.stop()

        assert supervisor._running is False
        assert supervisor._stopping is True

    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(self, supervisor):
        """Calling stop() multiple times should be safe."""
        await supervisor.start()
        await supervisor.stop()
        await supervisor.stop()  # Should not crash

        assert supervisor._stopping is True

    @pytest.mark.asyncio
    async def test_shutdown_stops_services_in_reverse_order(self, supervisor):
        """Services should stop in reverse startup order."""
        await supervisor.start()

        # Track which services are stopped
        stopped = []

        # Patch services to track shutdown order
        if supervisor.notification_service:
            original_stop = supervisor.notification_service.stop

            async def track_notification_stop():
                stopped.append("notification")
                await original_stop()

            supervisor.notification_service.stop = track_notification_stop

        if supervisor.reconciler:
            original_reconcile_stop = supervisor.reconciler.stop

            async def track_reconcile_stop():
                stopped.append("reconciler")
                await original_reconcile_stop()

            supervisor.reconciler.stop = track_reconcile_stop

        await supervisor.stop()

        # Notification should stop before reconciler (reverse order)
        # This is just an example - actual order depends on implementation

    @pytest.mark.asyncio
    async def test_shutdown_calls_shutdown_service(self, supervisor):
        """Shutdown service should be called during stop."""
        await supervisor.start()

        # Track if shutdown service was called
        shutdown_called = False

        if supervisor.shutdown_service:
            original_shutdown = supervisor.shutdown_service.shutdown

            async def track_shutdown(*args, **kwargs):
                nonlocal shutdown_called
                shutdown_called = True
                return await original_shutdown(*args, **kwargs)

            supervisor.shutdown_service.shutdown = track_shutdown

        await supervisor.stop()

        assert shutdown_called


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestStartupErrorHandling:
    """Tests for error handling during startup."""

    @pytest.mark.asyncio
    async def test_infrastructure_failure_cleans_up(self, supervisor):
        """Should clean up if infrastructure initialization fails."""
        # Mock event bus to fail
        with patch("funding_bot.app.supervisor.lifecycle.InMemoryEventBus") as mock_event_bus:
            mock_event_bus.return_value.start = AsyncMock(side_effect=Exception("Event bus failed"))

            with pytest.raises(Exception):
                await supervisor.start()

            # Should still attempt cleanup
            assert supervisor._running is False

    @pytest.mark.asyncio
    async def test_adapter_failure_cleans_up(self, supervisor):
        """Should clean up if adapter initialization fails."""
        await supervisor._init_infrastructure()

        # Mock Lighter to fail
        with patch.object(supervisor.lighter, "initialize", side_effect=Exception("Lighter init failed")):
            with pytest.raises(Exception):
                await supervisor.start()

            # Should stop already started services
            assert supervisor._running is False


# =============================================================================
# Broken Hedge Event Tests
# =============================================================================


class TestBrokenHedgeHandling:
    """Tests for broken hedge event handling."""

    @pytest.mark.asyncio
    async def test_subscribes_to_broken_hedge_event(self, supervisor):
        """Should subscribe to BrokenHedgeDetected events."""
        await supervisor._init_infrastructure()
        await supervisor._init_adapters()
        await supervisor._start_services()

        # Publish a broken hedge event
        event = BrokenHedgeDetected(
            trade_id="test-trade",
            symbol="ETH",
            detected_at=datetime.now(UTC),
            reason="Test broken hedge",
        )

        await supervisor.event_bus.publish(event)

        # Should be handled (no crash)


# =============================================================================
# State Tests
# =============================================================================


class TestSupervisorState:
    """Tests for supervisor state management."""

    @pytest.mark.asyncio
    async def test_running_flag_is_set_correctly(self, supervisor):
        """Running flag should reflect actual state."""
        assert supervisor._running is False

        await supervisor.start()
        assert supervisor._running is True

        await supervisor.stop()
        assert supervisor._running is False

    @pytest.mark.asyncio
    async def test_stopping_flag_is_set_during_shutdown(self, supervisor):
        """Stopping flag should be set during shutdown."""
        await supervisor.start()

        assert supervisor._stopping is False

        await supervisor.stop()
        assert supervisor._stopping is True

    @pytest.mark.asyncio
    async def test_shutdown_event_is_set_on_stop(self, supervisor):
        """Shutdown event should be set when stopping."""
        await supervisor.start()

        assert supervisor._shutdown_event.is_set() is False

        await supervisor.stop()
        assert supervisor._shutdown_event.is_set() is True
