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
    funding_tracker = FundingTracker(x10, lighter, state_manager, update_interval_seconds=3600)
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

    # Pre-load prices
    logger.info("📈 Pre-loading Lighter prices...")
    await lighter.load_funding_rates_and_prices()
    logger.info(f"✅ Lighter prices loaded: {len(lighter.price_cache)} symbols")
    
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
    logger.info(f"启动 OI Tracker für {len(common_symbols)} Symbole...")
    oi_tracker = await init_oi_tracker(x10, lighter, symbols=common_symbols)
    
    # Init WebSocket Manager
    logger.info("🌐 Starting WebSocket Manager...")
    ws_manager = await init_websocket_manager(
        x10, lighter, symbols=common_symbols,
        ping_interval=None, ping_timeout=None
    )
    ws_manager.set_oi_tracker(oi_tracker)
    logger.info("🔗 Components Wired: WS -> OI Tracker -> Prediction")

    # Zombie check
    logger.info("🧟 Starte Zombie-Check: Vergleiche DB mit echten Positionen...")
    try:
        db_trades = await get_open_trades()
    except Exception:
        db_trades = []

    if db_trades:
        logger.info(f"🔍 DB meldet {len(db_trades)} offene Trades. Prüfe Exchange...")
        try:
            real_x10_pos = await x10.fetch_open_positions()
            real_lighter_pos = await lighter.fetch_open_positions()
            
            def _get_sym(p):
                return p.get('symbol') if isinstance(p, dict) else getattr(p, 'symbol', None)

            real_symbols = []
            if real_x10_pos:
                real_symbols.extend([_get_sym(p) for p in real_x10_pos if _get_sym(p)])
            if real_lighter_pos:
                real_symbols.extend([_get_sym(p) for p in real_lighter_pos if _get_sym(p)])

            logger.info(f"🌍 Echte Positionen auf Exchanges: {real_symbols}")

            for trade in db_trades:
                tsym = trade.get('symbol') if isinstance(trade, dict) else getattr(trade, 'symbol', None)
                if tsym and tsym not in real_symbols:
                    logger.warning(f"⚠️  ZOMBIE: {tsym} - closing in DB...")
                    try:
                        await close_trade_in_state(tsym)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Zombie-Check failed: {e}")

    # Reverse ghost check
    logger.info("🧟 Reverse Ghost Check: Suche Positionen auf Exchange ohne DB Entry...")
    try:
        x10_positions = await x10.fetch_open_positions()
        lighter_positions = await lighter.fetch_open_positions()
        
        x10_syms = {p.get('symbol') for p in (x10_positions or [])}
        lighter_syms = {p.get('symbol') for p in (lighter_positions or [])}
        all_exchange_syms = x10_syms | lighter_syms
        
        db_syms = {t.get('symbol') if isinstance(t, dict) else getattr(t, 'symbol', None) for t in db_trades}
        orphaned = all_exchange_syms - db_syms
        
        if orphaned:
            logger.warning(f"🚨 Found {len(orphaned)} ORPHANED positions: {orphaned}")
            
            # Fix #12: Automatically close orphan positions at startup
            from src.utils import safe_float
            for symbol in orphaned:
                try:
                    # Close Lighter position if exists
                    lighter_pos = next((p for p in (lighter_positions or []) if p.get('symbol') == symbol), None)
                    if lighter_pos:
                        size = safe_float(lighter_pos.get('size', 0))
                        if abs(size) > 0:
                            logger.warning(f"🚨 Closing orphan Lighter position {symbol} (size={size}) at startup...")
                            px = safe_float(lighter.fetch_mark_price(symbol))
                            if px > 0:
                                notional = abs(size) * px
                                original_side = "BUY" if size < 0 else "SELL"
                                await lighter.close_live_position(symbol, original_side, notional)
                                logger.info(f"✅ Closed orphaned Lighter {symbol} at startup")
                    
                    # Close X10 position if exists
                    x10_pos = next((p for p in (x10_positions or []) if p.get('symbol') == symbol), None)
                    if x10_pos:
                        size = safe_float(x10_pos.get('size', 0))
                        if abs(size) > 0:
                            logger.warning(f"🚨 Closing orphan X10 position {symbol} (size={size}) at startup...")
                            px = safe_float(x10.fetch_mark_price(symbol))
                            if px > 0:
                                notional = abs(size) * px
                                original_side = "BUY" if size < 0 else "SELL"
                                await x10.close_live_position(symbol, original_side, notional)
                                logger.info(f"✅ Closed orphaned X10 {symbol} at startup")
                except Exception as e:
                    logger.error(f"❌ Failed to close orphan position {symbol} at startup: {e}")
        else:
            logger.info("✅ No orphaned positions found")
    except Exception as e:
        logger.debug(f"Ghost check error: {e}")

    # Init ParallelExecutionManager
    parallel_exec = ParallelExecutionManager(x10, lighter, state_manager)
    logger.info("✅ ParallelExecutionManager started")

    # Init Dashboard API
    start_time = time.time()
    api_server = DashboardApi(state_manager, parallel_exec, start_time)
    await api_server.start()
    
    # Setup Event Loop
    event_loop = get_event_loop()
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
        close_database_fn=close_database,
        stop_fee_manager_fn=stop_fee_manager,
    )
    
    # Register tasks
    event_loop.register_task(
        "logic_loop",
        lambda: logic_loop(lighter, x10, price_event, parallel_exec),
        priority=TaskPriority.HIGH,
        restart_on_failure=True
    )
    
    event_loop.register_task(
        "trade_management_loop",
        lambda: trade_management_loop(lighter, x10),
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
    
    try:
        await event_loop.start()
    except KeyboardInterrupt:
        logger.info("🛑 KeyboardInterrupt detected")
        raise
    except asyncio.CancelledError:
        logger.info("🛑 Shutdown requested (CancelledError)")
    finally:
        SHUTDOWN_FLAG = True
        logger.info("🛑 Shutting down...")
        
        if telegram_bot and telegram_bot.enabled:
            await telegram_bot.send_message("🛑 **Bot shutting down...**")
        
        await event_loop.stop()
    
    # Cleanup
    if api_server:
        await api_server.stop()
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
    
    bot = FundingBot()
    bot_task = asyncio.create_task(bot.run())
    
    try:
        await bot_task
    except asyncio.CancelledError:
        logger.info("Main bot task cancelled.")
    finally:
        logger.info("🛑 STOPPING BOT: Initiating Graceful Shutdown...")
        
        if hasattr(bot, 'graceful_shutdown'):
            await bot.graceful_shutdown()
        
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
