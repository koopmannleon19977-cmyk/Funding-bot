# src/core/startup.py
"""
Bot startup and initialization.

This module handles:
- Database setup and migration
- Bot initialization (adapters, managers)
- FundingBot class
- Entry points (main_entry, run_bot_v5)
"""

import asyncio
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Optional, List

import aiohttp
import aiosqlite

import config
from src.utils import safe_float
from src.reconciliation import reconcile_positions_atomic

# Logger is obtained from main.py's setup
logger = logging.getLogger(__name__)

# ============================================================
# GLOBALS (shared references)
# ============================================================
SHUTDOWN_FLAG = False
state_manager = None
telegram_bot = None


# ============================================================
# DATABASE FUNCTIONS
# ============================================================
async def setup_database():
    """Initialize async database"""
    from src.database import get_database
    db = await get_database()
    logger.info("✅ Async database initialized")


async def migrate_database():
    """Migrate DB schema for new columns"""
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            # Create trade_history table if it doesn't exist
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    entry_time TIMESTAMP,
                    exit_time TIMESTAMP,
                    hold_duration_hours REAL,
                    close_reason TEXT,
                    final_pnl_usd REAL,
                    funding_pnl_usd REAL,
                    spread_pnl_usd REAL,
                    fees_usd REAL,
                    account_label TEXT DEFAULT 'Main'
                )
            """)
            await conn.commit()
            
            # Check if migration needed
            cursor = await conn.execute("PRAGMA table_info(trade_history)")
            columns = await cursor.fetchall()
            has_account_label = any(col[1] == 'account_label' for col in columns)
            
            if not has_account_label:
                logger.info("🔄 Migrating database schema...")
                try:
                    await conn.execute("ALTER TABLE trade_history ADD COLUMN account_label TEXT DEFAULT 'Main'")
                except Exception:
                    pass
                try:
                    await conn.execute("ALTER TABLE trades ADD COLUMN account_label TEXT DEFAULT 'Main'")
                except Exception:
                    pass
                await conn.commit()
                logger.info("✅ Database migration complete")
            else:
                logger.debug("✅ Database schema up to date")
                
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        logger.warning("⚠️ Continuing without migration...")


async def close_all_open_positions_on_start(lighter, x10):
    """EMERGENCY: Close all open positions on bot start."""
    from src.core.state import get_open_trades, close_trade_in_state
    
    logger.warning("🚨 EMERGENCY: Closing ALL open positions...")
    
    open_trades = await get_open_trades()
    
    if not open_trades:
        logger.info("✓ No open positions to close")
        return
    
    logger.info(f"⚠️  Found {len(open_trades)} open positions, closing...")
    
    for trade in open_trades:
        symbol = trade['symbol']
        try:
            from src.position_manager import close_position_with_reason
            success = await close_position_with_reason(
                symbol,
                "EMERGENCY_CLEANUP_ON_START",
                lighter,
                x10
            )
            if success:
                logger.info(f"✓ Closed {symbol}")
            else:
                logger.error(f"✗ Failed to close {symbol}")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"✗ Error closing {symbol}: {e}")
    
    logger.info("✓ Emergency cleanup complete")


# ============================================================
# RUN BOT V5
# ============================================================
async def run_bot_v5(bot_instance=None):
    """
    Main bot entry point with full task supervision and component wiring.
    """
    global SHUTDOWN_FLAG, state_manager, telegram_bot
    
    logger.info("🔍 run_bot_v5() entry point called")
    
    # Lazy imports to avoid circular dependencies
    from src.adapters.x10_adapter import X10Adapter
    from src.adapters.lighter_adapter import LighterAdapter
    from src.state_manager import get_state_manager, close_state_manager
    from src.telegram_bot import get_telegram_bot
    from src.database import close_database
    from src.fee_manager import init_fee_manager, get_fee_manager, stop_fee_manager
    from src.funding_tracker import FundingTracker
    from src.parallel_execution import ParallelExecutionManager
    from src.event_loop import BotEventLoop, TaskPriority, get_event_loop
    from src.open_interest_tracker import init_oi_tracker
    from src.websocket_manager import init_websocket_manager
    from src.shutdown import get_shutdown_orchestrator
    from src.api_server import DashboardApi
    
    # Import loop functions from core modules (not main.py to avoid circular import!)
    from src.core.monitoring import (
        logic_loop, trade_management_loop, farm_loop, 
        maintenance_loop, connection_watchdog, cleanup_finished_tasks,
        health_reporter
    )
    from src.core.state import get_open_trades, close_trade_in_state
    
    logger.info("🔥 BOT V5 (Architected) STARTING...")
    
    # 1. INIT INFRASTRUCTURE
    state_manager = await get_state_manager()
    logger.info("✅ State Manager started")
    
    telegram_bot = get_telegram_bot()
    if telegram_bot.enabled:
        await telegram_bot.start()
        logger.info("📱 Telegram Bot connected")
        
    await setup_database()
    await migrate_database()
    
    x10 = X10Adapter()
    lighter = LighterAdapter()
    
    if bot_instance:
        bot_instance.x10 = x10
        bot_instance.lighter = lighter
    
    price_event = asyncio.Event()
    x10.price_update_event = price_event
    lighter.price_update_event = price_event
    
    # Balance check
    logger.info("💰 Checking exchange balances at startup...")
    try:
        await x10._get_trading_client()
        bal_x10 = await x10.get_real_available_balance()
        bal_lit = await lighter.get_real_available_balance()
        logger.info(f"💰 STARTUP BALANCE CHECK: X10=${bal_x10:.2f}, Lighter=${bal_lit:.2f}")
        
        if bal_x10 == 0 and bal_lit == 0:
            logger.critical("🚨 CRITICAL: BOTH exchange balances are $0!")
        else:
            logger.info("✅ Exchange balances OK - Bot can trade!")
    except Exception as e:
        logger.error(f"❌ STARTUP BALANCE CHECK FAILED: {e}", exc_info=True)
    
    # Init FeeManager
    fee_manager = await init_fee_manager(x10, lighter)
    logger.info("✅ FeeManager started")
    
    # Init FundingTracker
    interval = int(getattr(config, "FUNDING_TRACK_INTERVAL_SECONDS", 300))
    funding_tracker = FundingTracker(x10, lighter, state_manager, update_interval_seconds=interval)
    await funding_tracker.start()
    logger.info("✅ FundingTracker started")
    
    # Load market data
    logger.info("📊 Loading Market Data via REST...")
    try:
        await asyncio.gather(
            x10.load_market_cache(force=True),
            lighter.load_market_cache(force=True),
            return_exceptions=True
        )
        logger.info(f"✅ Markets loaded: X10={len(x10.market_info)}, Lighter={len(lighter.market_info)}")
    except Exception as e:
        logger.error(f"⚠️ Market load warning: {e}")

    # Warmup execution clients
    logger.info("🔥 Warming up execution clients (Network Handshake)...")
    try:
        await lighter._get_signer()
        x10_client = await x10._get_trading_client()
        await x10_client.markets_info.get_markets()
        logger.info("✅ Execution clients warmed up & NETWORK READY")
    except Exception as e:
        logger.warning(f"⚠️ Warmup warning: {e}")

    # Init OI Tracker
    common_symbols = list(set(x10.market_info.keys()) & set(lighter.market_info.keys()))
    
    # ═══════════════════════════════════════════════════════════════
    # Initialize Stream Clients for real-time updates
    # ═══════════════════════════════════════════════════════════════
    logger.info("🌐 Initializing Stream Clients...")

    if getattr(config, "USE_ADAPTER_STREAM_CLIENTS", False):
        # Initialize X10 Stream Client
        try:
            await x10.initialize_stream_client(symbols=common_symbols)
            logger.info("X10 stream client initialized")
        except Exception as e:
            logger.warning(f"X10 stream client initialization failed: {e}")
            logger.info("Continuing with polling fallback...")

        # Initialize Lighter Stream Client
        try:
            await lighter.initialize_stream_client(symbols=common_symbols)
            logger.info("Lighter stream client initialized")
        except Exception as e:
            logger.warning(f"Lighter stream client initialization failed: {e}")
            logger.info("Continuing with polling fallback...")
    else:
        logger.info("Adapter stream clients disabled (using WebSocketManager)")

    # Init OI Tracker
    logger.info(f"启动 OI Tracker für {len(common_symbols)} Symbole...")
    oi_tracker = await init_oi_tracker(x10, lighter, symbols=common_symbols)
    
    # Init WebSocket Manager
    logger.info("🌐 Starting WebSocket Manager...")
    ws_manager = await init_websocket_manager(
        x10, lighter, symbols=common_symbols,
        ping_interval=None, ping_timeout=None
    )
    ws_manager.set_oi_tracker(oi_tracker)
    # Prefer WS for real-time prices/funding. Use REST only as a fallback.
    ws_wait = float(getattr(config, "LIGHTER_WAIT_FOR_WS_MARKET_STATS_SECONDS", 10.0))
    ws_ready = False
    if hasattr(lighter, "wait_for_ws_market_stats_ready"):
        logger.info("📈 Waiting for Lighter WS market_stats...")
        ws_ready = await lighter.wait_for_ws_market_stats_ready(timeout=ws_wait)
    if ws_ready:
        logger.info("✅ Lighter WS market_stats ready - skipping REST preload")
    elif getattr(config, "LIGHTER_STARTUP_REST_FALLBACK", True):
        logger.info("📈 Pre-loading Lighter prices (REST fallback)...")
        await lighter.load_funding_rates_and_prices(force=True)
        logger.info(f"✅ Lighter prices loaded: {len(lighter.price_cache)} symbols")
    else:
        logger.info("⚠️ Lighter WS market_stats not ready; REST preload disabled")
    logger.info("🔗 Components Wired: WS -> OI Tracker -> Prediction")

    # ═══════════════════════════════════════════════════════════════
    # RECONCILIATION (Zombie & Ghost Fix)
    # ═══════════════════════════════════════════════════════════════
    logger.info("⚖️ Starting Atomic Reconciliation (Adoption Mode)...")
    try:
        recon_result = await reconcile_positions_atomic(
            state_manager=state_manager,
            x10_adapter=x10,
            lighter_adapter=lighter,
            auto_fix=True
        )
        
        if recon_result.errors:
            logger.error(f"❌ Reconciliation errors: {recon_result.errors}")
        else:
            logger.info(
                f"✅ Reconciliation Complete: "
                f"Matched={len(recon_result.matched)}, "
                f"ZombiesFixed={len(recon_result.zombies_found)}, "
                f"GhostsAdopted={len(recon_result.ghosts_found)}"
            )
            
    except Exception as e:
        logger.error(f"❌ Critical Reconciliation Failure: {e}", exc_info=True)

    # Init ParallelExecutionManager
    parallel_exec = ParallelExecutionManager(x10, lighter, state_manager)

    # Init Dashboard API
    logger.info("🌐 Initializing Dashboard API...")
    start_time = time.time()
    api_server = None
    try:
        api_server = DashboardApi(state_manager, parallel_exec, start_time)
        logger.info("🌐 Starting Dashboard API server...")
        started = await api_server.start()

        if started:

            logger.info("✅ Dashboard API started")

        else:

            logger.warning("?s???? Continuing without Dashboard API...")

            api_server = None
    except Exception as e:
        logger.error(f"❌ Failed to start Dashboard API: {e}", exc_info=True)
        logger.warning("⚠️ Continuing without Dashboard API...")
        api_server = None
    
    # Setup Event Loop
    logger.info("🔧 Setting up Event Loop...")
    event_loop = get_event_loop()
    logger.info(f"✅ Event Loop obtained: {type(event_loop).__name__}")
    event_loop.x10_adapter = x10
    event_loop.lighter_adapter = lighter
    event_loop.parallel_exec = parallel_exec
    event_loop.ws_manager = ws_manager
    event_loop.state_manager = state_manager
    event_loop.telegram_bot = telegram_bot

    # Wire shutdown orchestrator
    shutdown = get_shutdown_orchestrator()
    shutdown.configure(
        x10=x10,
        lighter=lighter,
        ws_manager=ws_manager,
        parallel_exec=parallel_exec,
        state_manager=state_manager,
        telegram_bot=telegram_bot,
        oi_tracker=oi_tracker,  # FIX: Add OI Tracker for proper shutdown
        funding_tracker=funding_tracker,  # FIX: Add Funding Tracker for final update
        close_database_fn=close_database,
        stop_fee_manager_fn=stop_fee_manager,
    )
    
    # Register tasks
    logger.info("📝 Registering tasks with Event Loop...")
    event_loop.register_task(
        "logic_loop",
        lambda: logic_loop(lighter, x10, price_event, parallel_exec),
        priority=TaskPriority.HIGH,
        restart_on_failure=True
    )
    
    from src.core.trade_management import manage_open_trades
    event_loop.register_task(
        "trade_management_loop",
        lambda: trade_management_loop(lighter, x10, manage_open_trades),
        priority=TaskPriority.HIGH,
        restart_on_failure=True
    )
    
    event_loop.register_task(
        "farm_loop",
        lambda: farm_loop(lighter, x10, parallel_exec),
        priority=TaskPriority.NORMAL,
        restart_on_failure=True
    )
    
    event_loop.register_task(
        "maintenance_loop",
        lambda: maintenance_loop(lighter, x10, parallel_exec),
        priority=TaskPriority.LOW,
        restart_on_failure=True
    )
    
    event_loop.register_task(
        "cleanup_finished_tasks",
        lambda: cleanup_finished_tasks(),
        priority=TaskPriority.LOW,
        restart_on_failure=True
    )
    
    event_loop.register_task(
        "health_reporter",
        lambda: health_reporter(event_loop, parallel_exec),
        priority=TaskPriority.LOW,
        restart_on_failure=True
    )
    
    event_loop.register_task(
        "connection_watchdog",
        lambda: connection_watchdog(ws_manager, x10, lighter),
        priority=TaskPriority.CRITICAL,
        restart_on_failure=True
    )
    
    # Start event loop
    if telegram_bot and telegram_bot.enabled:
        await telegram_bot.send_message(
            "🚀 **Funding Bot V5 Started**\n"
            f"OI Tracker: Active ({len(common_symbols)} syms)\n"
            "Mode: Centralized Event Loop"
        )
    
    logger.info("═══════════════════════════════════════════════════════════")
    logger.info("   BOT V5 RUNNING 24/7 - SUPERVISED | Ctrl+C = Stop   ")
    logger.info("═══════════════════════════════════════════════════════════")
    # Get task count safely
    task_count = len(getattr(event_loop, '_tasks', {}))
    logger.info(f"🔍 About to call event_loop.start() - tasks registered: {task_count}")
    
    # #region agent log
    import json
    DEBUG_LOG_PATH = r"c:\Users\koopm\funding-bot\.cursor\debug.log"
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H1", "location": "startup.py:398", "message": "Before event_loop.start()", "data": {"event_loop": str(event_loop), "event_loop_running": event_loop.is_running() if hasattr(event_loop, "is_running") else None}, "timestamp": int(time.time() * 1000)}) + "\n")
    except: pass
    # #endregion
    
    try:
        # #region agent log
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H1", "location": "startup.py:410", "message": "Calling event_loop.start()", "data": {}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        await event_loop.start()
        # #region agent log
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H1", "location": "startup.py:416", "message": "event_loop.start() returned", "data": {}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
    except KeyboardInterrupt as e:
        # #region agent log
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H4", "location": "startup.py:420", "message": "KeyboardInterrupt in event_loop.start()", "data": {"error": str(e)}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        logger.info("🛑 KeyboardInterrupt detected")
        raise
    except asyncio.CancelledError as e:
        # #region agent log
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H4", "location": "startup.py:428", "message": "CancelledError in event_loop.start()", "data": {"error": str(e)}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        logger.info("🛑 Shutdown requested (CancelledError)")
    except Exception as e:
        # #region agent log
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "debug-session", "runId": "run1", "hypothesisId": "H4", "location": "startup.py:436", "message": "Exception in event_loop.start()", "data": {"error": str(e), "error_type": type(e).__name__}, "timestamp": int(time.time() * 1000)}) + "\n")
        except: pass
        # #endregion
        logger.error(f"❌ Exception in event_loop.start(): {e}", exc_info=True)
        raise
    finally:
        SHUTDOWN_FLAG = True
        logger.info("🛑 Shutting down...")
        
        if telegram_bot and telegram_bot.enabled:
            await telegram_bot.send_message("🛑 **Bot shutting down...**")
        
        await event_loop.stop()
    
    # Cleanup
    # #region agent log
    import json
    DEBUG_LOG_PATH = r"c:\Users\koopm\funding-bot\.cursor\debug.log"
    def _write_debug_log(entry):
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
    _write_debug_log({
        "sessionId": "debug-session",
        "runId": "port-binding-debug",
        "hypothesisId": "B",
        "location": "startup.py:finally",
        "message": "Before api_server.stop()",
        "data": {"api_server_exists": api_server is not None},
        "timestamp": int(time.time() * 1000)
    })
    # #endregion
    if api_server:
        await api_server.stop()
    # #region agent log
    _write_debug_log({
        "sessionId": "debug-session",
        "runId": "port-binding-debug",
        "hypothesisId": "B",
        "location": "startup.py:finally",
        "message": "After api_server.stop()",
        "data": {},
        "timestamp": int(time.time() * 1000)
    })
    # #endregion
    await parallel_exec.stop()
    if ws_manager:
        await ws_manager.stop()
    if oi_tracker:
        await oi_tracker.stop()
    if funding_tracker:
        await funding_tracker.stop()
    await close_state_manager()
    if telegram_bot and telegram_bot.enabled:
        await telegram_bot.stop()
    await close_database()
    await stop_fee_manager()
    
    logger.info("🔌 Closing adapters...")
    # Stop Stream Clients before closing adapters
    try:
        await x10.stop_stream_client()
    except Exception as e:
        logger.warning(f"⚠️ Error stopping X10 Stream Client: {e}")
    
    try:
        await lighter.stop_stream_client()
    except Exception as e:
        logger.warning(f"⚠️ Error stopping Lighter Stream Client: {e}")
    
    await x10.aclose()
    await lighter.aclose()
    
    logger.info("✅ Bot V5 shutdown complete")


# ============================================================
# FUNDING BOT CLASS
# ============================================================
class FundingBot:
    """Main bot class with graceful shutdown support."""
    
    def __init__(self):
        self.x10 = None
        self.lighter = None
        self._running = False

    async def run(self):
        """Main run method - calls run_bot_v5."""
        self._running = True
        await run_bot_v5(bot_instance=self)
        self._running = False

    async def graceful_shutdown(self):
        """Execute graceful shutdown - close all positions."""
        global state_manager
        
        from src.shutdown import get_shutdown_orchestrator
        from src.database import close_database
        from src.fee_manager import stop_fee_manager
        
        logger.info("🛑 GRACEFUL SHUTDOWN: Closing all positions...")
        
        shutdown = get_shutdown_orchestrator()
        shutdown.configure(
            x10=self.x10,
            lighter=self.lighter,
            ws_manager=None,
            parallel_exec=None,
            state_manager=state_manager,
            telegram_bot=telegram_bot,
            close_database_fn=close_database,
            stop_fee_manager_fn=stop_fee_manager,
        )

        result = await asyncio.shield(shutdown.shutdown(reason="funding_bot"))
        if not result.get("success"):
            logger.warning(f"Shutdown completed with issues: {result}")
        else:
            logger.info("✅ All positions closed. Bye!")


# ============================================================
# ENTRY POINT
# ============================================================
async def main_entry():
    """Main entry point with clean shutdown handling."""
    global state_manager
    
    from src.database import close_database
    from src.fee_manager import stop_fee_manager
    
    logger.info("🚀 main_entry() called - creating bot task...")
    bot = FundingBot()
    bot_task = asyncio.create_task(bot.run())
    logger.info("✅ Bot task created, waiting for completion...")
    
    try:
        await bot_task
        logger.info("✅ Bot task completed normally")
    except asyncio.CancelledError:
        logger.info("Main bot task cancelled.")
    except Exception as e:
        logger.error(f"❌ Exception in bot task: {e}", exc_info=True)
        raise
    finally:
        # ═══════════════════════════════════════════════════════════════
        # FIX: run_bot_v5 already handles graceful shutdown in its finally block
        # Calling graceful_shutdown() again here causes duplicate position close attempts
        # The shutdown orchestrator now has a completion flag to prevent this
        # ═══════════════════════════════════════════════════════════════
        logger.info("🛑 Main entry: Bot task completed")
        
        # Final cleanup
        logger.info("🛑 Stopping Infrastructure...")
        try:
            if state_manager:
                await state_manager.stop()
            await close_database()
            await stop_fee_manager()
            
            if bot and bot.x10:
                await bot.x10.aclose()
            if bot and bot.lighter:
                await bot.lighter.aclose()
        except Exception as e:
            logger.debug(f"Infrastructure cleanup error: {e}")


def run():
    """Synchronous entry point for running the bot."""
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main_entry())
    except KeyboardInterrupt:
        print("\n\n🚨 STRG+C erkannt! Fahre herunter...")
    except Exception as e:
        print(f"CRITICAL MAIN FAILURE: {e}")
        traceback.print_exc()
