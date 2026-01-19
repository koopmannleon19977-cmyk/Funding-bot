"""
Startup and shutdown lifecycle management.

These functions are designed to be used as methods of the Supervisor class.
They are defined externally and assigned to the class in manager.py.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from funding_bot.domain.events import BrokenHedgeDetected
from funding_bot.observability.logging import get_logger

if TYPE_CHECKING:
    from funding_bot.app.supervisor.manager import Supervisor

logger = get_logger(__name__)


async def start(self: Supervisor) -> None:
    """Start all services in correct order."""
    if self._running:
        logger.warning("Supervisor already running")
        return

    logger.info("Supervisor starting...")
    self._running = True
    self._stopping = False

    try:
        # Phase 1: Initialize infrastructure
        await self._init_infrastructure()

        # Phase 2: Initialize adapters
        await self._init_adapters()

        # Phase 3: Start services
        await self._start_services()

        # Phase 4: Start main loops
        await self._start_loops()

        logger.info("Supervisor started successfully")

    except Exception as e:
        logger.exception(f"Supervisor start failed: {e}")
        await self.stop()
        raise


async def stop(self: Supervisor) -> None:
    """Stop all services gracefully."""
    if self._stopping:
        logger.debug("Supervisor already stopping")
        return

    self._stopping = True
    self._running = False
    logger.info("Supervisor stopping...")

    # Signal shutdown
    self._shutdown_event.set()

    # Cancel all tasks with proper exception handling
    await self._cancel_all_tasks()

    # Stop services in reverse order
    await self._stop_services()

    # Best-effort shutdown safety: cancel open orders and clean up orphaned OPENING/PENDING trades.
    # This prevents leftover orders from filling after shutdown and avoids "stuck" trades on next start.
    if self.shutdown_service:
        with contextlib.suppress(Exception):
            shutdown_cfg = getattr(self.settings, "shutdown", None)
            close_positions = bool(
                getattr(shutdown_cfg, "close_positions_on_exit", False) if shutdown_cfg is not None else False
            )
            timeout = float(getattr(shutdown_cfg, "timeout_seconds", 20.0) if shutdown_cfg is not None else 20.0)
            await self.shutdown_service.shutdown(
                close_positions=close_positions,
                timeout=timeout,
            )

    # Close adapters
    await self._close_adapters()

    # Close infrastructure
    await self._close_infrastructure()

    logger.info("Supervisor stopped")


async def _init_infrastructure(self: Supervisor) -> None:
    """Initialize database and other infrastructure."""
    logger.info("Initializing infrastructure...")

    # Create directories
    Path(self.settings.database.path).parent.mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    Path("data").mkdir(exist_ok=True)

    # Initialize event bus
    from funding_bot.adapters.messaging.event_bus import InMemoryEventBus

    self.event_bus = InMemoryEventBus()
    await self.event_bus.start()

    # 🚀 PERFORMANCE: Initialize REST API pool for connection pooling
    from funding_bot.utils.rest_pool import initialize_global_pool

    await initialize_global_pool()
    # Note: rest_pool.py logs "REST API pool initialized" with details

    # Initialize store
    from funding_bot.adapters.store.sqlite import SQLiteTradeStore

    self.store = SQLiteTradeStore(self.settings)
    await self.store.initialize()

    logger.info("Infrastructure initialized")


async def _init_adapters(self: Supervisor) -> None:
    """Initialize exchange adapters."""
    logger.info("Initializing adapters...")

    from funding_bot.adapters.exchanges.lighter import LighterAdapter

    self.lighter = LighterAdapter(self.settings)

    # X10 Adapter (optional - SDK may not be installed)
    try:
        from funding_bot.adapters.exchanges.x10 import X10Adapter

        self.x10 = X10Adapter(self.settings)
        await self.x10.initialize()
        # Note: adapter.py logs "X10 adapter initialized with N markets"
    except ImportError:
        logger.warning("X10 SDK not installed. Bot will run with Lighter only.")
        logger.warning("Install X10 SDK with: pip install x10-python-trading-starknet")
        self.x10 = None
    except Exception as e:
        logger.error(f"Failed to initialize X10 adapter: {e}")
        logger.warning("Bot will run with Lighter only.")
        self.x10 = None

    # Telegram Adapter
    from funding_bot.adapters.messaging.telegram import TelegramAdapter

    self.notifier = TelegramAdapter(self.settings.telegram)

    await self.lighter.initialize()
    # self.notifier.start() is handled by NotificationService

    logger.info("Adapters initialized")


async def _start_services(self: Supervisor) -> None:
    """Start all business services."""
    logger.info("Starting services...")

    from funding_bot.services.execution import ExecutionEngine
    from funding_bot.services.funding import FundingTracker
    from funding_bot.services.market_data import MarketDataService
    from funding_bot.services.notification import NotificationService
    from funding_bot.services.opportunities import OpportunityEngine
    from funding_bot.services.positions import PositionManager
    from funding_bot.services.reconcile import Reconciler
    from funding_bot.services.shutdown import ShutdownService

    # Market Data Service
    self.market_data = MarketDataService(
        settings=self.settings,
        lighter=self.lighter,
        x10=self.x10,
    )
    await self.market_data.start()

    # Opportunity Engine
    self.opportunity_engine = OpportunityEngine(
        settings=self.settings,
        market_data=self.market_data,
        store=self.store,
    )

    # Execution Engine
    self.execution_engine = ExecutionEngine(
        settings=self.settings,
        lighter=self.lighter,
        x10=self.x10,
        store=self.store,
        event_bus=self.event_bus,
        market_data=self.market_data,
    )

    # Position Manager
    self.position_manager = PositionManager(
        settings=self.settings,
        lighter=self.lighter,
        x10=self.x10,
        store=self.store,
        event_bus=self.event_bus,
        market_data=self.market_data,
        opportunity_engine=self.opportunity_engine,
    )

    # Funding Tracker
    self.funding_tracker = FundingTracker(
        settings=self.settings,
        lighter=self.lighter,
        x10=self.x10,
        store=self.store,
        event_bus=self.event_bus,
        market_data=self.market_data,
    )
    await self.funding_tracker.start()

    # Reconciler
    self.reconciler = Reconciler(
        settings=self.settings,
        lighter=self.lighter,
        x10=self.x10,
        store=self.store,
        event_bus=self.event_bus,
    )
    await self.reconciler.start()

    # Shutdown Service
    self.shutdown_service = ShutdownService(
        settings=self.settings,
        lighter=self.lighter,
        x10=self.x10,
        store=self.store,
    )

    # Notification Service
    self.notification_service = NotificationService(
        settings=self.settings,
        event_bus=self.event_bus,
        notifier=self.notifier,
    )
    await self.notification_service.start()

    # Historical Data Ingestion Service (optional)
    hist_cfg = getattr(self.settings, "historical", None)
    if hist_cfg and getattr(hist_cfg, "enabled", False):
        from funding_bot.services.historical.ingestion import HistoricalIngestionService

        self.historical_ingestion = HistoricalIngestionService(
            store=self.store,
            settings=self.settings,
            lighter_adapter=self.lighter,
        )
        logger.info("Historical data ingestion service initialized")

    # Subscribe to BrokenHedgeDetected events for global trading pause
    self.event_bus.subscribe(BrokenHedgeDetected, self._on_broken_hedge)

    # Proactively start X10 Account WebSocket if we have open trades
    # This enables faster order/position updates during trade management.
    # Without this, the WS only starts on first new trade execution.
    try:
        open_trades = await self.store.list_open_trades()
        if open_trades and self.execution_engine:
            logger.info(f"Proactively starting WS subscriptions for {len(open_trades)} open trades...")
            await self.execution_engine._ensure_ws_order_subscriptions()
    except Exception as e:
        logger.warning(f"Failed to proactively start WS subscriptions (non-fatal): {e}")

    logger.info("Services started")


async def _stop_services(self: Supervisor) -> None:
    """Stop all services."""
    logger.info("Stopping services...")

    # Stop in reverse order of startup
    if self.notification_service:
        await self.notification_service.stop()

    if self.reconciler:
        await self.reconciler.stop()

    if self.funding_tracker:
        await self.funding_tracker.stop()

    if self.market_data:
        await self.market_data.stop()

    logger.info("Services stopped")


async def _close_adapters(self: Supervisor) -> None:
    """Close exchange adapters."""
    logger.info("Closing adapters...")

    if self.lighter:
        await self.lighter.close()

    if self.x10:
        await self.x10.close()

    if self.notifier:
        await self.notifier.stop()

    logger.info("Adapters closed")


async def _close_infrastructure(self: Supervisor) -> None:
    """Close database and infrastructure."""
    logger.info("Closing infrastructure...")

    if self.store:
        await self.store.close()

    if self.event_bus:
        await self.event_bus.stop()

    # 🚀 PERFORMANCE: Close REST API pool
    from funding_bot.utils.rest_pool import close_global_pool

    await close_global_pool()
    # Note: rest_pool.py logs "REST API pool closed"

    logger.info("Infrastructure closed")
