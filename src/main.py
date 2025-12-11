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

import sys
import os
import time
import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Set, Tuple
from decimal import Decimal

# ============================================================
# SETUP PATH FIRST
# ============================================================
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# ============================================================
# LOGGING SETUP - MUST BE BEFORE ALL OTHER IMPORTS!
# ============================================================
import config

# Initialize logging ONCE at the very start
logger = config.setup_logging(per_run=True, run_id=os.getenv("RUN_ID"))
config.validate_runtime_config(logger)

# Noise Reduction for verbose modules
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger('fee_manager').setLevel(logging.WARNING)
logging.getLogger('position_cache').setLevel(logging.WARNING)

# ============================================================
# NOW IMPORT OTHER MODULES (after logging is setup)
# ============================================================
from src.telegram_bot import get_telegram_bot
from src.api_server import DashboardApi
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.latency_arb import get_detector, is_latency_arb_enabled
from src.state_manager import (
    InMemoryStateManager,
    get_state_manager,
    close_state_manager,
    TradeState,
    TradeStatus
)
from src.adaptive_threshold import get_threshold_manager
from src.volatility_monitor import get_volatility_monitor
from src.fee_manager import get_fee_manager, init_fee_manager, stop_fee_manager
from src.parallel_execution import ParallelExecutionManager
from src.account_manager import get_account_manager
from src.websocket_manager import WebSocketManager

from src.kelly_sizing import get_kelly_sizer, calculate_smart_size, KellyResult
from src.database import (
    get_database,
    get_trade_repository,
    get_funding_repository,
    get_execution_repository,
    close_database
)
# from src.funding_tracker import create_funding_tracker
from src.utils import safe_float, safe_decimal, quantize_usd

# ============================================================
# GLOBALS
# ============================================================
FAILED_COINS = {}
ACTIVE_TASKS: dict[str, asyncio.Task] = {}
SYMBOL_LOCKS: dict[str, asyncio.Lock] = {}
SHUTDOWN_FLAG = False
POSITION_CACHE = {'x10': [], 'lighter': [], 'last_update': 0.0}
POSITION_CACHE_TTL = 10.0
LOCK_MANAGER_LOCK = asyncio.Lock()
EXECUTION_LOCKS = {}
TASKS_LOCK = asyncio.Lock()
OPPORTUNITY_LOG_CACHE = {}
LAST_ARBITRAGE_LAUNCH = 0.0
LAST_DATA_UPDATE = time.time()
WATCHDOG_TIMEOUT = 120
RECENTLY_OPENED_TRADES: Dict[str, float] = {}
RECENTLY_OPENED_LOCK = asyncio.Lock()
RECENTLY_OPENED_PROTECTION_SECONDS = 60.0
IN_FLIGHT_MARGIN = {'X10': 0.0, 'Lighter': 0.0}
IN_FLIGHT_LOCK = asyncio.Lock()

# State Manager
state_manager = None
telegram_bot = None

# ============================================================
# IMPORT ALL FUNCTIONS FROM CORE MODULES
# ============================================================
from src.core import (
    # State
    get_open_trades,
    add_trade_to_state,
    close_trade_in_state,
    archive_trade_to_history,
    get_cached_positions,
    get_execution_lock,
    get_symbol_lock,
    get_local_state_manager,
    check_total_exposure,
    # Opportunities
    find_opportunities,
    calculate_expected_profit,
    is_tradfi_or_fx,
    # Trading
    execute_trade_parallel,
    execute_trade_task,
    launch_trade_task,
    close_trade,
    safe_close_x10_position,
    get_actual_position_size,
    process_symbol,
    # Trade Management
    sync_check_and_fix,
    cleanup_zombie_positions,
    calculate_trade_age,
    calculate_realized_pnl,
    should_farm_quick_exit,
    parse_iso_time,
    manage_open_trades,
    reconcile_db_with_exchanges,
    reconcile_state_with_exchange,
    # Monitoring
    connection_watchdog,
    cleanup_finished_tasks,
    emergency_position_cleanup,
    farm_loop,
    trade_management_loop,
    maintenance_loop,
    health_reporter,
    balance_watchdog,
    logic_loop,
    # Startup
    run_bot_v5,
    FundingBot,
    main_entry,
    setup_database,
    migrate_database,
    close_all_open_positions_on_start,
)

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
                        fee_rate=fee_rate if fee_rate > 0 else config.TAKER_FEE_X10
                    )
                return

            await asyncio.sleep(2)

        except Exception as e:
            if retry == 1:
                fallback = config.TAKER_FEE_X10 if adapter.name == 'X10' else 0.0
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
    return config.TAKER_FEE_X10 if exchange == 'X10' else 0.0


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    # Windows-Fix für Event Loop
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main_entry())
    except KeyboardInterrupt:
        print("\n\n🚨 STRG+C erkannt! Fahre herunter (bitte warten)...")
    except Exception as e:
        print(f"CRITICAL MAIN FAILURE: {e}")
        traceback.print_exc()
