"""
Entry points for bot commands.

Each command sets up the environment and runs the appropriate logic.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from pathlib import Path

# Load .env file BEFORE importing settings
from dotenv import load_dotenv

# Try multiple paths to find .env
for env_path in [
    Path.cwd() / ".env",  # Current working directory
    Path(__file__).parent.parent.parent.parent / ".env",  # Project root
]:
    if env_path.exists():
        load_dotenv(env_path)
        break
else:
    load_dotenv()  # Fallback: search default locations

from funding_bot.config.settings import get_settings  # noqa: E402
from funding_bot.observability.logging import get_logger, setup_logging  # noqa: E402


def _log_startup_banner(logger, *, env: str, mode: str, settings) -> None:
    logger.warning("========================================================")
    if mode == "live":
        logger.warning("STARTING IN LIVE MODE (REAL ORDERS ENABLED)")
    else:
        logger.warning("STARTING IN PAPER MODE (NO REAL ORDERS)")
    logger.warning(f"env={env} | db={settings.database.path} | telegram={settings.telegram.enabled}")
    logger.warning(
        "risk: "
        f"max_exposure_pct={settings.risk.max_exposure_pct} "
        f"max_trade_size_pct={settings.risk.max_trade_size_pct} "
        f"max_drawdown_pct={settings.risk.max_drawdown_pct} "
        f"min_free_margin_pct={settings.risk.min_free_margin_pct}"
    )
    logger.warning(
        "trading: "
        f"desired_notional_usd={settings.trading.desired_notional_usd} "
        f"max_open_trades={settings.trading.max_open_trades} "
        f"min_apy_filter={settings.trading.min_apy_filter} "
        f"max_spread_filter_percent={settings.trading.max_spread_filter_percent}"
    )
    logger.warning("========================================================")


async def run_bot(
    env: str = "development",
    *,
    mode_override: str | None = None,
    confirm_live: bool = False,
    allow_testing_live: bool = False,
) -> int:
    """
    Main bot entry point.

    Initializes the bot infrastructure and starts the supervisor loop.
    Handles graceful shutdown via signal handlers (SIGINT, SIGTERM).

    Args:
        env: Environment name for configuration loading ("development", "production").
        mode_override: Force "paper" or "live" mode, overriding config.
        confirm_live: Set True to confirm live trading (safety check).
        allow_testing_live: Allow live trading even in testing_mode (dangerous).

    Returns:
        Exit code: 0 = success, 1 = fatal error, 2 = configuration error.

    Example:
        >>> asyncio.run(run_bot(env="production", confirm_live=True))

    Note:
        On Windows, signal handling runs in the main thread only.
        The handler sets a shutdown event without logging (reentrant-safe).
    """
    # Load settings
    settings = get_settings(env)

    from funding_bot.app.run_safety import RunMode, apply_run_safety

    typed_mode_override: RunMode | None = None
    if mode_override is not None:
        if mode_override not in ("paper", "live"):
            raise ValueError(f"Invalid mode_override={mode_override!r}")
        # mode_override is validated to be "paper" or "live", matching RunMode Literal
        typed_mode_override = mode_override  # type: ignore[assignment]

    decision = apply_run_safety(
        settings,
        mode_override=typed_mode_override,
        confirm_live_flag=confirm_live,
        allow_testing_live_flag=allow_testing_live,
    )
    settings = decision.settings

    # Setup logging
    setup_logging(settings)
    logger = get_logger(__name__)

    _log_startup_banner(logger, env=env, mode=decision.mode, settings=settings)

    for warning in decision.warnings:
        logger.warning(warning)

    if decision.errors:
        for error in decision.errors:
            logger.error(error)
        logger.error("Aborting startup due to run-safety errors.")
        return 2

    # Import here to avoid circular imports
    from funding_bot.app.supervisor import Supervisor

    supervisor = Supervisor(settings)

    # Signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()
    received_signal: list[str] = []  # Store signal name for logging after handler

    def handle_signal(sig: signal.Signals) -> None:
        # NOTE: On Unix, this runs in the event loop context, so logging is safe
        received_signal.append(sig.name)
        logger.info(f"Received signal {sig.name}, initiating shutdown...")
        shutdown_event.set()

    # Register signal handlers
    if sys.platform == "win32":
        # Windows: use signal.signal (runs in main thread only)
        # IMPORTANT: Do NOT log in signal handler - causes reentrant write errors
        # when signal arrives during an active log write to stdout
        def win_handler(signum: int, frame) -> None:
            received_signal.append(f"signal-{signum}")
            shutdown_event.set()

        signal.signal(signal.SIGINT, win_handler)
        signal.signal(signal.SIGTERM, win_handler)
    else:
        # Unix: use loop.add_signal_handler for async-safe handling
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))

    try:
        # Start supervisor
        await supervisor.start()

        # Wait for shutdown signal or supervisor completion
        await shutdown_event.wait()

        # Log the signal AFTER the handler returns (safe from reentrant issues)
        if received_signal:
            logger.info(f"Received {received_signal[0]}, initiating shutdown...")

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown signal received, shutting down...")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1
    finally:
        await supervisor.stop()

    logger.info("Bot stopped cleanly")
    return 0


async def run_doctor() -> int:
    """Run preflight checks."""
    import os

    settings = get_settings()
    setup_logging(settings)
    logger = get_logger(__name__)

    logger.info("Running preflight checks...")

    checks_passed = 0
    checks_failed = 0

    # Check 1: Lighter credentials (check both settings and env)
    lighter_key = settings.lighter.private_key or os.getenv("LIGHTER_PRIVATE_KEY", "")
    if lighter_key:
        logger.info("[OK] Lighter private key configured")
        checks_passed += 1
    else:
        logger.error("[FAIL] Lighter private key not configured")
        checks_failed += 1

    # Check 2: X10 credentials (check both settings and env)
    x10_api = settings.x10.api_key or os.getenv("X10_API_KEY", "")
    x10_private = settings.x10.private_key or os.getenv("X10_PRIVATE_KEY", "")
    if x10_api and x10_private:
        logger.info("[OK] X10 credentials configured")
        checks_passed += 1
    else:
        logger.error("[FAIL] X10 credentials not configured")
        checks_failed += 1

    # Check 3: Database directory
    from pathlib import Path

    db_dir = Path(settings.database.path).parent
    if db_dir.exists() or not settings.database.path:
        logger.info("[OK] Database directory exists")
        checks_passed += 1
    else:
        db_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[OK] Database directory created")
        checks_passed += 1

    # Check 4: Logs directory
    logs_dir = Path("logs")
    if logs_dir.exists():
        logger.info("[OK] Logs directory exists")
        checks_passed += 1
    else:
        logs_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[OK] Logs directory created")
        checks_passed += 1

    # Summary
    logger.info(f"\nPreflight: {checks_passed} passed, {checks_failed} failed")

    return 0 if checks_failed == 0 else 1


async def run_reconcile() -> int:
    """Manually reconcile DB with exchange positions."""
    settings = get_settings()
    setup_logging(settings)
    logger = get_logger(__name__)

    logger.warning("Manual reconciliation starting (assumes bot is not running)")

    import contextlib

    from funding_bot.adapters.exchanges.lighter import LighterAdapter
    from funding_bot.adapters.exchanges.x10 import X10Adapter
    from funding_bot.adapters.messaging.event_bus import InMemoryEventBus
    from funding_bot.adapters.store.sqlite import SQLiteTradeStore
    from funding_bot.services.reconcile import Reconciler

    lighter = LighterAdapter(settings)
    x10 = X10Adapter(settings)
    store = SQLiteTradeStore(settings)
    event_bus = InMemoryEventBus()

    try:
        await store.initialize()
        await event_bus.start()
        await lighter.initialize()
        await x10.initialize()

        reconciler = Reconciler(
            settings=settings,
            lighter=lighter,
            x10=x10,
            store=store,
            event_bus=event_bus,
        )
        result = await reconciler.reconcile(startup=True)

        if result.success:
            logger.info(
                "Reconciliation complete: "
                f"zombies_closed={result.zombies_closed} "
                f"ghosts_closed={result.ghosts_closed} "
                f"ghosts_adopted={result.ghosts_adopted}"
            )
            return 0

        for err in result.errors:
            logger.error(f"Reconciliation error: {err}")
        return 1
    except Exception as e:
        logger.exception(f"Manual reconciliation failed: {e}")
        return 1
    finally:
        with contextlib.suppress(Exception):
            await event_bus.stop()
        with contextlib.suppress(Exception):
            await lighter.close()
        with contextlib.suppress(Exception):
            await x10.close()
        with contextlib.suppress(Exception):
            await store.close()


async def run_close_all() -> int:
    """Emergency: close all positions on both exchanges.

    This uses the ShutdownService to:
    1. Cancel all open orders
    2. Close all positions
    3. Mark trades as CLOSED in database
    """
    settings = get_settings()
    setup_logging(settings)
    logger = get_logger(__name__)

    logger.warning("========================================")
    logger.warning("EMERGENCY CLOSE-ALL INITIATED")
    logger.warning("========================================")

    try:
        # Initialize adapters
        from funding_bot.adapters.exchanges.lighter import LighterAdapter
        from funding_bot.adapters.exchanges.x10 import X10Adapter
        from funding_bot.adapters.store.sqlite import SQLiteTradeStore
        from funding_bot.services.shutdown import ShutdownService

        lighter = LighterAdapter(settings)
        x10 = X10Adapter(settings)
        store = SQLiteTradeStore(settings)

        await store.initialize()
        await lighter.initialize()
        await x10.initialize()

        logger.info("Adapters initialized")

        # Create shutdown service
        shutdown_service = ShutdownService(
            settings=settings,
            lighter=lighter,
            x10=x10,
            store=store,
        )

        # Execute emergency shutdown with position closing
        logger.warning("Executing emergency shutdown...")
        await shutdown_service.shutdown(
            close_positions=True,
            timeout=60.0,
        )

        logger.warning("========================================")
        logger.warning("EMERGENCY CLOSE-ALL COMPLETE")
        logger.warning("========================================")

        # Cleanup
        await lighter.close()
        await x10.close()
        await store.close()

        return 0

    except Exception as e:
        logger.exception(f"Emergency close-all failed: {e}")
        return 1


async def run_backfill(
    env: str = "development",
    *,
    days: int = 90,
    symbols: list[str] | None = None,
) -> int:
    """Backfill historical funding data from both exchanges.

    This command fetches minute-level candle data from the past N days
    and stores it in the database for APY crash detection and analysis.

    Args:
        env: Environment to use (development or prod)
        days: Number of days to backfill (default: 90)
        symbols: List of symbols to backfill (default: common symbols)

    Returns:
        0 on success, 1 on failure
    """
    settings = get_settings(env)
    setup_logging(settings)
    logger = get_logger(__name__)

    logger.warning("========================================")
    logger.warning("HISTORICAL DATA BACKFILL INITIATED")
    logger.warning("========================================")
    logger.warning(f"Days: {days}")
    logger.warning(f"Symbols: {symbols or 'ALL AVAILABLE MARKETS (default)'}")

    # Check if historical analysis is enabled
    hist_cfg = getattr(settings, "historical", None)
    if not hist_cfg or not getattr(hist_cfg, "enabled", False):
        logger.error("Historical analysis is disabled in config.yaml")
        logger.error("Set 'historical.enabled: true' to use backfill")
        return 1

    # Default symbols: Load ALL available markets if none provided
    if not symbols:
        logger.warning("No symbols specified - loading all available markets from exchanges...")

        # Initialize adapters to load markets
        from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
        from funding_bot.adapters.exchanges.x10.adapter import X10Adapter

        all_symbols = set()

        # Load Lighter markets
        try:
            lighter_adapter = LighterAdapter(settings)
            await lighter_adapter.initialize()
            await lighter_adapter.load_markets()
            lighter_symbols = set(lighter_adapter._markets.keys())
            all_symbols.update(lighter_symbols)
            logger.warning(f"  ✓ Loaded {len(lighter_symbols)} markets from Lighter")
            await lighter_adapter.close()
        except Exception as e:
            logger.warning(f"  ✗ Failed to load Lighter markets: {e}")

        # Load X10 markets
        try:
            x10_adapter = X10Adapter(settings)
            await x10_adapter.initialize()
            await x10_adapter.load_markets()
            x10_symbols = set(x10_adapter._markets.keys())
            all_symbols.update(x10_symbols)
            logger.warning(f"  ✓ Loaded {len(x10_symbols)} markets from X10")
            await x10_adapter.close()
        except Exception as e:
            logger.warning(f"  ✗ Failed to load X10 markets: {e}")

        # Fallback to hardcoded symbols if both exchanges fail
        if not all_symbols:
            logger.warning("Both exchanges failed - using fallback symbols")
            symbols = ["ETH", "BTC", "SOL", "DOGE", "PEPE"]
        else:
            symbols = sorted(list(all_symbols))
            logger.warning(f"  ✓ Total: {len(symbols)} markets to backfill")

    try:
        # Initialize store
        from funding_bot.adapters.store.sqlite import SQLiteTradeStore
        from funding_bot.services.historical.ingestion import HistoricalIngestionService

        store = SQLiteTradeStore(settings)
        await store.initialize()

        # Create ingestion service with adapters for dynamic market ID mapping
        from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
        from funding_bot.adapters.exchanges.x10.adapter import X10Adapter

        lighter_adapter = None
        x10_adapter = None

        try:
            lighter_adapter = LighterAdapter(settings)
            await lighter_adapter.initialize()
        except Exception as e:
            logger.warning(f"Lighter adapter init failed: {e}")

        try:
            x10_adapter = X10Adapter(settings)
            await x10_adapter.initialize()
        except Exception as e:
            logger.warning(f"X10 adapter init failed: {e}")

        ingestion = HistoricalIngestionService(
            store=store,
            settings=settings,
            lighter_adapter=lighter_adapter,
        )

        logger.info(f"Starting backfill for {len(symbols)} symbols...")

        # Run backfill
        results = await ingestion.backfill(symbols, days_back=days)

        # Report results
        total_records = sum(results.values())
        logger.warning("========================================")
        logger.warning("BACKFILL COMPLETE")
        logger.warning("========================================")
        logger.warning(f"Total records inserted: {total_records}")

        for symbol, count in results.items():
            status = "OK" if count > 0 else "SKIP"
            logger.warning(f"  [{status}] {symbol}: {count:,} records")

        # Close adapters
        if lighter_adapter:
            try:
                await lighter_adapter.close()
            except Exception as e:
                logger.warning(f"Failed to close Lighter adapter: {e}")

        if x10_adapter:
            try:
                await x10_adapter.close()
            except Exception as e:
                logger.warning(f"Failed to close X10 adapter: {e}")

        # Close store
        await store.close()

        return 0

    except Exception as e:
        logger.exception(f"Backfill failed: {e}")

        # Cleanup on error
        if lighter_adapter:
            with contextlib.suppress(Exception):
                await lighter_adapter.close()

        if x10_adapter:
            with contextlib.suppress(Exception):
                await x10_adapter.close()

        return 1
