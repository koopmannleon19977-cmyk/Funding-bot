# monitor_funding.py - REFACTORED
"""
Funding Bot Entry Point.

All business logic has been moved to src/core/ modules:
- src/core/state.py: State management and DB wrappers
- src/core/opportunities.py: Opportunity detection
- src/core/trading.py: Trade execution
- src/core/trade_management.py: Open trade management
- src/core/monitoring.py: Background loops and watchdogs
- src/core/startup.py: Bot initialization
"""

import asyncio
import logging
import os
import sys
import time
import traceback

# ============================================================
# SETUP PATH FIRST
# ============================================================
project_root = os.path.abspath(os.path.dirname(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# ============================================================
# LOGGING SETUP - MUST BE BEFORE ALL OTHER IMPORTS!
# ============================================================
import config

# Initialize logging ONCE at the very start
logger = config.setup_logging(per_run=True, run_id=os.getenv("RUN_ID"))
config.validate_runtime_config(logger)

# Initialize JSON Logger (B5: Structured logging for Grafana/ELK)
from src.utils.json_logger import JSONLogger, LogLevel

json_logger = None
if getattr(config, "JSON_LOGGING_ENABLED", False):
    log_level_str = getattr(config, "JSON_LOG_MIN_LEVEL", "INFO")
    log_level = LogLevel[log_level_str.upper()] if log_level_str else LogLevel.INFO
    json_log_file = getattr(config, "JSON_LOG_FILE", None)
    json_logger = JSONLogger.get_instance(log_file=json_log_file, enabled=True, min_level=log_level)
    logger.info(f"✅ JSON Logger initialized: {json_log_file or 'default'}")

# Noise Reduction for verbose modules
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("fee_manager").setLevel(logging.WARNING)
logging.getLogger("position_cache").setLevel(logging.WARNING)

# ============================================================
# NOW IMPORT OTHER MODULES (after logging is setup)
# ============================================================
from src.application.fee_manager import stop_fee_manager
from src.infrastructure.database import (
    close_database,
)
from src.infrastructure.telegram_bot import get_telegram_bot

# from src.application.funding_tracker import create_funding_tracker

# ============================================================
# GLOBALS
# ============================================================
FAILED_COINS = {}
ACTIVE_TASKS: dict[str, asyncio.Task] = {}
SYMBOL_LOCKS: dict[str, asyncio.Lock] = {}
SHUTDOWN_FLAG = False
POSITION_CACHE = {"x10": [], "lighter": [], "last_update": 0.0}
POSITION_CACHE_TTL = 10.0
LOCK_MANAGER_LOCK = asyncio.Lock()
EXECUTION_LOCKS = {}
TASKS_LOCK = asyncio.Lock()
OPPORTUNITY_LOG_CACHE = {}
LAST_ARBITRAGE_LAUNCH = 0.0
LAST_DATA_UPDATE = time.time()
WATCHDOG_TIMEOUT = 120
RECENTLY_OPENED_TRADES: dict[str, float] = {}
RECENTLY_OPENED_LOCK = asyncio.Lock()
RECENTLY_OPENED_PROTECTION_SECONDS = 120.0  # FIX (2025-12-19): Extended to 120s to match zombie check & allow for Exchange API latency (Lighter takes 3-5s to show new positions)
IN_FLIGHT_MARGIN = {"X10": 0.0, "Lighter": 0.0}
IN_FLIGHT_LOCK = asyncio.Lock()

# State Manager
state_manager = None
telegram_bot = None

# ============================================================
# IMPORT ALL FUNCTIONS FROM CORE MODULES
# ============================================================
from src.core import (
    # ... other imports ...
    FundingBot,
)
from src.core.events import CriticalError, NotificationEvent
from src.infrastructure.event_bus import EventBus


async def main_entry():
    """Main entry point with clean shutdown handling and EventBus setup."""

    logger.info("🚀 main_entry() called - setting up EventBus...")

    # 1. Setup EventBus
    event_bus = EventBus()
    await event_bus.start()

    # 2. Setup Telegram handler
    telegram = get_telegram_bot()
    if telegram and telegram.enabled:
        await telegram.start()

        async def telegram_handler(event):
            if isinstance(event, CriticalError):
                await telegram.send_error(f"🚨 CRITICAL: {event.message}\nDetails: {event.details}")
            elif isinstance(event, NotificationEvent):
                if event.level in ["ERROR", "CRITICAL"]:
                    await telegram.send_error(f"⚠️ {event.level}: {event.message}")
                else:
                    await telegram.send_message(f"ℹ️ {event.level}: {event.message}")

        event_bus.subscribe(CriticalError, telegram_handler)
        event_bus.subscribe(NotificationEvent, telegram_handler)
        logger.info("📱 Telegram handler subscribed to EventBus")

    logger.info("🚀 Creating bot task...")
    bot = FundingBot()
    bot_task = asyncio.create_task(bot.run(event_bus=event_bus))
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
        logger.info("🛑 Main entry: Bot task completed")

        # Stop EventBus
        await event_bus.stop()

        # Final cleanup
        # ═══════════════════════════════════════════════════════════════
        # FIX (2025-12-22): Use close_state_manager() instead of get_state_manager()
        # get_state_manager() creates a NEW instance if _state_manager is None,
        # which causes the "Starting InMemoryStateManager" log during shutdown.
        # close_state_manager() safely closes the existing instance if it exists.
        # ═══════════════════════════════════════════════════════════════
        logger.info("🛑 Stopping Infrastructure...")
        try:
            from src.infrastructure.state_manager import close_state_manager

            await close_state_manager()
            await close_database()
            await stop_fee_manager()

            if bot and bot.x10:
                await bot.x10.aclose()
            if bot and bot.lighter:
                await bot.lighter.aclose()
        except Exception as e:
            logger.debug(f"Infrastructure cleanup error: {e}")


# ============================================================
# HELPER FUNCTIONS (kept here for backwards compatibility)
# ============================================================
def safe_int(val, default=0):
    """Convert any value to int safely."""
    if val is None or val == "" or val == "None":
        return default
    if isinstance(val, int):
        return val
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return default


async def load_fee_cache():
    """Load historical fee stats (no-op hook)."""
    return


async def process_fee_update(adapter, symbol: str, order_id: str):
    """Fetch order fee after execution and update stats."""
    if not order_id or order_id == "DRY_RUN":
        return

    await asyncio.sleep(config.SLEEP_LONG)

    for retry in range(2):
        try:
            fee_rate = await adapter.get_order_fee(order_id)

            if fee_rate > 0 or retry == 1:
                if state_manager:
                    await state_manager.update_fee_stats(
                        exchange=adapter.name,
                        symbol=symbol,
                        fee_rate=fee_rate if fee_rate > 0 else config.TAKER_FEE_X10,
                    )
                return

            await asyncio.sleep(2)

        except Exception:
            if retry == 1:
                fallback = config.TAKER_FEE_X10 if adapter.name == "X10" else 0.0
                if state_manager:
                    await state_manager.update_fee_stats(adapter.name, symbol, fallback)
            await asyncio.sleep(1)


def get_estimated_fee_rate(exchange: str, symbol: str) -> float:
    """Get cached fee rate from state manager."""
    if state_manager:
        try:
            return state_manager.get_fee_estimate(exchange, symbol)
        except Exception:
            pass
    return config.TAKER_FEE_X10 if exchange == "X10" else 0.0


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    # Windows-Fix für Event Loop
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main_entry())
    except KeyboardInterrupt:
        print("\n\n🚨 STRG+C erkannt! Fahre herunter (bitte warten)...")
    except Exception as e:
        print(f"CRITICAL MAIN FAILURE: {e}")
        traceback.print_exc()
