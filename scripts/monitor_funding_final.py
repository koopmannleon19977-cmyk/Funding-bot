# monitor_funding.py
import sys
import os
import time
import asyncio
import aiosqlite
import inspect
import logging
import random
import traceback
import aiohttp  # ← HINZUFÜGEN
import signal
from datetime import datetime, timezone
from typing import Optional, List, Dict, Callable, Awaitable, Any, Set
from decimal import Decimal
from dataclasses import dataclass, field
from enum import Enum

# ============================================================
# IMPORTS & SETUP
# ============================================================
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import config
from src.telegram_bot import get_telegram_bot
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.prediction_v2 import get_predictor
from src.latency_arb import get_detector
from src.state_manager import (
    InMemoryStateManager,
    get_state_manager,
    close_state_manager,
    TradeState,
    TradeStatus
)
from src.adaptive_threshold import get_threshold_manager
from src.volatility_monitor import get_volatility_monitor
from src.parallel_execution import ParallelExecutionManager
from src.account_manager import get_account_manager
from src.websocket_manager import WebSocketManager
from src.prediction import predict_next_funding_rates
from src.kelly_sizing import get_kelly_sizer, calculate_smart_size, KellyResult
from src.database import (
    get_database,
    get_trade_repository,
    get_funding_repository,
    get_execution_repository,
    close_database
)

# --- Noise Reduction: limit verbose subsystem logs BEFORE setup_logging ---
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
# --------------------------------------------------------------------------

# Logging Setup
logger = config.setup_logging(per_run=True, run_id=os.getenv("RUN_ID"))
config.validate_runtime_config(logger)

# Globals
FAILED_COINS = {}
ACTIVE_TASKS: dict[str, asyncio.Task] = {}           # symbol → Task (nur eine pro Symbol gleichzeitig)
SYMBOL_LOCKS: dict[str, asyncio.Lock] = {}           # symbol → asyncio.Lock() für race-free execution
SHUTDOWN_FLAG = False
POSITION_CACHE = {'x10': [], 'lighter': [], 'last_update': 0.0}
POSITION_CACHE_TTL = 5.0  # 5s for fresher data (was 30s)
LOCK_MANAGER_LOCK = asyncio.Lock()
EXECUTION_LOCKS = {}
TASKS_LOCK = asyncio.Lock()
OPPORTUNITY_LOG_CACHE = {}
LAST_ARBITRAGE_LAUNCH = 0.0  # Time of last arbitrage trade launch

# ============================================================
# CONNECTION WATCHDOG (Gegen Ping Timeout)
# ============================================================
LAST_DATA_UPDATE = time.time()  # Global timestamp für letzte Datenaktivität
WATCHDOG_TIMEOUT = 120  # Sekunden ohne Daten = Verbindungsproblem

# ============================================================
# GLOBALS (Ergänzung)
# ============================================================
# Behalte IN_FLIGHT_MARGIN von vorher!
# Define in-flight reservation globals (used when reserving trade notional)
IN_FLIGHT_MARGIN = {'X10': 0.0, 'Lighter': 0.0}
IN_FLIGHT_LOCK = asyncio.Lock()
# ============================================================
# PREDICTION SYSTEM STUBS
# ============================================================
async def update_funding_data(symbol: str, rate_x10: float, rate_lighter: float, 
                               mark_price: float, open_interest: float):
    """Stub for prediction system - data already cached in adapters"""
    pass

async def get_best_opportunities(symbols: list, min_apy: float, min_confidence: float, limit: int):
    """Stub - returns empty list, main logic uses find_opportunities fallback"""
    return []

# ============================================================
# HELPER FUNCTIONS
# ============================================================
def safe_float(val, default=0.0):
    """Convert any value to float safely - handles API strings."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0):
    """Convert any value to int safely - handles API strings."""
    if val is None or val == "" or val == "None":
        return default
    if isinstance(val, int):
        return val
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return default

# ============================================================
# FEE & STARTUP LOGIC
# ============================================================
async def load_fee_cache():
    """Load historical fee stats from state manager on startup

    Note: The `InMemoryStateManager.start()` now loads fee stats into
    the running state; this function is kept as a no-op compatibility hook.
    """
    # State manager handles fee cache loading during startup
    return

# `update_fee_stats` removed: `state_manager.update_fee_stats()` is used instead.


async def process_fee_update(adapter, symbol: str, order_id: str):
    """Fetch order fee after execution and update stats via state manager."""
    if not order_id or order_id == "DRY_RUN":
        return

    # Wait for order to settle
    await asyncio.sleep(3.0)

    for retry in range(2):
        try:
            fee_rate = await adapter.get_order_fee(order_id)

            if fee_rate > 0 or retry == 1:
                # Update via state manager
                if state_manager:
                    await state_manager.update_fee_stats(
                        exchange=adapter.name,
                        symbol=symbol,
                        fee_rate=fee_rate if fee_rate > 0 else config.TAKER_FEE_X10
                    )
                return

            # Retry if zero (order might not be settled)
            await asyncio.sleep(2)

        except Exception as e:
            if retry == 1:
                # Fallback on final retry
                fallback = config.TAKER_FEE_X10 if adapter.name == 'X10' else 0.0
                if state_manager:
                    await state_manager.update_fee_stats(adapter.name, symbol, fallback)
            await asyncio.sleep(1)

async def process_symbol(symbol: str, lighter: LighterAdapter, x10: X10Adapter, 
                          parallel_exec: ParallelExecutionManager, lock: asyncio.Lock):
    """Einzelne Symbol-Verarbeitung mit OI-Integration"""
    async with lock:
        try:
            # Quick guards: blacklist, recent failures
            if symbol in config.BLACKLIST_SYMBOLS:
                logger.debug(f"{symbol}: in BLACKLIST, skip")
                return

            if symbol in FAILED_COINS and (time.time() - FAILED_COINS[symbol] < 300):
                logger.debug(f"{symbol}: in FAILED_COINS cooldown, skip")
                return

            # Check if already have open position
            open_trades = await get_open_trades()
            open_symbols = {t['symbol'] for t in open_trades}
            if symbol in open_symbols:
                logger.debug(f"{symbol}: already have open position, skip")
                return

            # Check max open trades limit
            if len(open_trades) >= config.MAX_OPEN_TRADES:
                logger.debug(f"{symbol}: MAX_OPEN_TRADES ({config.MAX_OPEN_TRADES}) reached, skip")
                return

            # Prices and funding rates (cached fetches)
            px = x10.fetch_mark_price(symbol)
            pl = lighter.fetch_mark_price(symbol)
            rl = lighter.fetch_funding_rate(symbol)
            rx = x10.fetch_funding_rate(symbol)

            if rl is None or rx is None:
                logger.debug(f"{symbol}: missing funding rates (rl={rl}, rx={rx})")
                return

            if px is None or pl is None:
                logger.debug(f"{symbol}: missing prices (px={px}, pl={pl})")
                return

            # Update volatility monitor with latest price
            try:
                get_volatility_monitor().update_price(symbol, safe_float(px))
            except Exception:
                pass

            net = rl - rx
            apy = abs(net) * 24 * 365  # Rates sind jetzt Hourly -> 24x am Tag

            # Threshold check
            threshold_manager = get_threshold_manager()
            req_apy = threshold_manager.get_threshold(symbol, is_maker=True)
            if apy < req_apy:
                logger.debug(f"{symbol}: APY {apy*100:.2f}% < required {req_apy*100:.2f}%")
                return

            # Price validity & spread
            try:
                px_f = safe_float(px)
                pl_f = safe_float(pl)
                if px_f <= 0 or pl_f <= 0:
                    logger.debug(f"{symbol}: invalid prices px={px_f}, pl={pl_f}")
                    return
                spread = abs(px_f - pl_f) / px_f
                if spread > config.MAX_SPREAD_FILTER_PERCENT:
                    logger.debug(f"{symbol}: spread {spread*100:.2f}% > max {config.MAX_SPREAD_FILTER_PERCENT*100:.2f}%")
                    return
            except Exception as e:
                logger.debug(f"{symbol}: price parsing error: {e}")
                return

            # ============================================================
            # Open Interest Check
            # ============================================================
            oi_x10 = 0.0
            oi_lighter = 0.0
            try:
                oi_x10 = await x10.fetch_open_interest(symbol)
            except Exception:
                pass
            try:
                oi_lighter = await lighter.fetch_open_interest(symbol)
            except Exception:
                pass
            
            total_oi = oi_x10 + oi_lighter
            
            # OI-basierte Validierung: Skip wenn OI zu niedrig (illiquide)
            min_oi_usd = getattr(config, 'MIN_OPEN_INTEREST_USD', 50000)
            if total_oi > 0 and total_oi < min_oi_usd:
                logger.debug(f"{symbol}: OI ${total_oi:.0f} < min ${min_oi_usd:.0f}")
                return

            # Build opportunity payload
            opp = {
                'symbol': symbol,
                'apy': apy * 100,
                'net_funding_hourly': net,
                'leg1_exchange': 'Lighter' if rl > rx else 'X10',
                'leg1_side': 'SELL' if rl > rx else 'BUY',
                'is_farm_trade': False,
                'spread_pct': spread,
                'open_interest': total_oi,
                'prediction_confidence': 0.5
            }

            logger.info(f"✅ {symbol}: EXECUTING trade APY={opp['apy']:.2f}% spread={spread*100:.2f}% OI=${total_oi:.0f}")

            # Execute using the deeper routine that handles reservations & cleanup
            await execute_trade_parallel(opp, lighter, x10, parallel_exec)

        except Exception as e:
            logger.error(f"Fehler bei Verarbeitung von {symbol}: {e}", exc_info=e)
            if telegram_bot and telegram_bot.enabled:
                asyncio.create_task(telegram_bot.send_error(f"Symbol Error {symbol}: {e}"))
        finally:
            # WICHTIG: Task aus ACTIVE_TASKS entfernen → cleanup kann laufen
            ACTIVE_TASKS.pop(symbol, None)
def get_estimated_fee_rate(exchange: str, symbol: str) -> float:
    """Get cached fee rate from state manager"""
    if state_manager:
        try:
            return state_manager.get_fee_estimate(exchange, symbol)
        except Exception:
            # If state manager call fails, fall back to defaults below
            pass

    # Fallback if state manager not initialized
    return config.TAKER_FEE_X10 if exchange == 'X10' else 0.0

telegram_bot = None

# ============================================================
# HELPERS
# ============================================================
async def get_execution_lock(symbol: str) -> asyncio.Lock:
    async with LOCK_MANAGER_LOCK:
        if symbol not in EXECUTION_LOCKS:
            EXECUTION_LOCKS[symbol] = asyncio.Lock()
        return EXECUTION_LOCKS[symbol]

# Add small helper to return the global state manager instance used elsewhere
def get_local_state_manager():
    """Return the global state manager instance (sync helper)"""
    return state_manager

# ============================================================
# STATE & DB WRAPPERS
# ============================================================
state_manager = None

async def get_open_trades() -> list:
    """Get open trades from in-memory state (instant)"""
    global state_manager
    if state_manager is None:
        # Import the REAL async get_state_manager from state_manager module
        from src.state_manager import get_state_manager as get_sm
        state_manager = await get_sm()
    sm = state_manager
    trades = await sm.get_all_open_trades()
    # Convert to dict format for backwards compatibility
    return [t.to_dict() for t in trades]

async def add_trade_to_state(trade_data: dict) -> str:
    """Add trade to in-memory state (writes to DB in background)"""
    sm = await get_state_manager()
    trade = TradeState(
        symbol=trade_data['symbol'],
        side_x10=trade_data.get('side_x10', 'BUY'),
        side_lighter=trade_data.get('side_lighter', 'SELL'),
        size_usd=trade_data.get('size_usd', 0),
        entry_price_x10=trade_data.get('entry_price_x10', 0),
        entry_price_lighter=trade_data.get('entry_price_lighter', 0),
        is_farm_trade=trade_data.get('is_farm_trade', False),
        account_label=trade_data.get('account_label', 'Main'),
        x10_order_id=trade_data.get('x10_order_id'),
        lighter_order_id=trade_data.get('lighter_order_id'),
    )
    return await sm.add_trade(trade)

async def close_trade_in_state(symbol: str, pnl: float = 0, funding: float = 0):
    """Close trade in state (writes to DB in background)"""
    sm = await get_state_manager()
    await sm.close_trade(symbol, pnl, funding)
    logger.info(f"✅ Trade {symbol} closed (PnL: ${pnl:.2f})")

async def archive_trade_to_history(trade_data: Dict, close_reason: str, pnl_data: Dict):
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            exit_time = datetime.utcnow()
            entry_time = trade_data.get('entry_time')
            if isinstance(entry_time, str):
                try:
                    entry_time = datetime.strptime(entry_time, '%Y-%m-%d %H:%M:%S.%f')
                except Exception:
                    try:
                        entry_time = datetime.strptime(entry_time, '%Y-%m-%d %H:%M:%S')
                    except Exception:
                        entry_time = datetime.utcnow()
            elif not isinstance(entry_time, datetime):
                entry_time = datetime.utcnow()
            
            duration = (exit_time - entry_time).total_seconds() / 3600 if entry_time else 0
            
            await conn.execute("""
                INSERT INTO trade_history 
                (symbol, entry_time, exit_time, hold_duration_hours, close_reason, 
                 final_pnl_usd, funding_pnl_usd, spread_pnl_usd, fees_usd, account_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['symbol'], entry_time, exit_time, duration, close_reason,
                pnl_data['total_net_pnl'], pnl_data['funding_pnl'], pnl_data['spread_pnl'], pnl_data['fees'],
                trade_data.get('account_label', 'Main')
            ))
            await conn.commit()
            logger.info(f" 💰 PnL {trade_data['symbol']}: ${pnl_data['total_net_pnl']:.2f} ({close_reason})")
    except Exception as e:
        logger.error(f"Archive Error: {e}")
        return False

    return True

# ============================================================
# CORE TRADING LOGIC
# ============================================================
def is_tradfi_or_fx(symbol: str) -> bool:
    s = symbol.upper().replace("-USD", "").replace("/", "")
    if s.startswith(("XAU", "XAG", "XBR", "WTI", "PAXG")): return True
    if s.startswith(("EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "TRY")) and "EUROC" not in s: return True
    if s.startswith(("SPX", "NDX", "US30", "DJI", "NAS")): return True
    return False

async def find_opportunities(lighter, x10, open_syms, is_farm_mode: bool = None) -> List[Dict]:
    """
    Find trading opportunities

    Args:
        lighter: Lighter adapter
        x10: X10 adapter
        open_syms: Set of already open symbols
        is_farm_mode: If True, mark all trades as farm trades. If None, auto-detect from config.
    """
    # Auto-detect farm mode if not specified
    if is_farm_mode is None:
        is_farm_mode = config.VOLUME_FARM_MODE

    opps: List[Dict] = []
    common = set(lighter.market_info.keys()) & set(x10.market_info.keys())
    threshold_manager = get_threshold_manager()
    detector = get_detector()  # ⚡ Latency Detector Instance

    mode_indicator = "🚜 FARM" if is_farm_mode else "💎 ARB"
    
    # Verify market data is loaded
    if not common:
        logger.warning("⚠️ No common markets found")
        logger.debug(f"X10 markets: {len(x10.market_info)}, Lighter: {len(lighter.market_info)}")
        return []

    # ═══════════════════════════════════════════════════════════════
    # WARMUP CHECK: Wait for WebSocket to fill caches
    # ═══════════════════════════════════════════════════════════════
    x10_rates_count = len(getattr(x10, 'funding_cache', {}))
    lighter_rates_count = len(getattr(lighter, 'funding_cache', {}))
    
    if x10_rates_count < 10 or lighter_rates_count < 10:
        logger.info(f"❄️ Warming up caches (waiting for WS data)... X10={x10_rates_count}, Lit={lighter_rates_count}")
        # Return empty list to skip this cycle and prevent false 'missing rates' logs
        return []
    # ═══════════════════════════════════════════════════════════════
    
    logger.info(f"🔍 {mode_indicator} Scanning {len(common)} pairs. Open symbols to skip: {open_syms}")
    
    # Verify price cache has data
    x10_prices = len([s for s in common if x10.fetch_mark_price(s) is not None])
    lit_prices = len([s for s in common if lighter.fetch_mark_price(s) is not None])
    
    logger.debug(f"Price cache status: X10={x10_prices}/{len(common)}, Lighter={lit_prices}/{len(common)}")
    
    if x10_prices == 0 and lit_prices == 0:
        logger.warning("⚠️ Price cache completely empty - WebSocket streams may not be working")
        # Force reload
        await asyncio.gather(
            x10.load_market_cache(force=True),
            lighter.load_market_cache(force=True),
            lighter.load_funding_rates_and_prices()
        )

    logger.info(
        f"🔍 Scanning {len(common)} pairs. "
        f"Lighter markets: {len(lighter.market_info)}, X10 markets: {len(x10.market_info)}"
    )

    semaphore = asyncio.Semaphore(10)  # Reduced from 20 to avoid rate limits

    async def fetch_symbol_data(s: str):
        async with semaphore:
            try:
                await asyncio.sleep(0.05)
                
                # Get funding rates (from cache, updated by maintenance_loop)
                lr = lighter.fetch_funding_rate(s)
                xr = x10.fetch_funding_rate(s)
                
                # Get prices (from cache, updated by WebSocket)
                px = x10.fetch_mark_price(s)
                pl = lighter.fetch_mark_price(s)
                
                # Log missing data for debugging
                if lr is None or xr is None:
                    logger.debug(f"{s}: Missing rates (L={lr}, X={xr})")
                if px is None or pl is None:
                    logger.debug(f"{s}: Missing prices (X={px}, L={pl})")
                
                return (s, lr, xr, px, pl)
                
            except Exception as e:
                logger.debug(f"Error fetching {s}: {e}")
                return (s, None, None, None, None)

    # Launch concurrent fetchs
    tasks = [asyncio.create_task(fetch_symbol_data(s)) for s in common]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out exceptions
    clean_results = []
    x10_rates = {}
    lighter_rates = {}
    
    for r in results:
        if isinstance(r, Exception):
            logger.debug(f"Task exception: {r}")
            continue
        clean_results.append(r)
        
        # Build rate dictionaries for prediction system
        s, lr, xr, px, pl = r
        if xr is not None:
            x10_rates[s] = xr
        if lr is not None:
            lighter_rates[s] = lr

    # Update Predictor with current data
    all_symbols = list(common)
    for symbol in all_symbols:
        rate_x10 = x10_rates.get(symbol, 0)
        rate_lighter = lighter_rates.get(symbol, 0)
        
        if rate_x10 != 0 or rate_lighter != 0:
            await update_funding_data(
                symbol=symbol,
                rate_x10=rate_x10,
                rate_lighter=rate_lighter,
                mark_price=lighter.fetch_mark_price(symbol) or 0,
                open_interest=0  # TODO: Add OI when available
            )
    
    # Get predictions
    predictions = await get_best_opportunities(
        symbols=[s for s in all_symbols if s not in open_syms],
        min_apy=getattr(config, 'MIN_APY_THRESHOLD', 10.0),
        min_confidence=0.5,
        limit=20
    )
    
    # Convert predictions to opportunity format
    opportunities = []
    for pred in predictions:
        if not pred.should_trade:
            continue
        
        opp = {
            'symbol': pred.symbol,
            'apy': pred.predicted_apy,
            'spread': pred.current_spread,
            'confidence': pred.confidence,
            'predicted_direction': pred.predicted_direction,
            'size_multiplier': pred.recommended_size_multiplier,
            'regime': pred.regime.value,
            'signals': pred.signals,
            # Existing fields for compatibility
            'rate_x10': x10_rates.get(pred.symbol, 0),
            'rate_lighter': lighter_rates.get(pred.symbol, 0),
            'is_farm_trade': is_farm_mode or False,
            'net_funding_hourly': lighter_rates.get(pred.symbol, 0) - x10_rates.get(pred.symbol, 0),
            'leg1_exchange': 'Lighter' if pred.predicted_direction == "long_lighter" else 'X10',
            'spread_pct': pred.current_spread,
            'price_x10': x10.fetch_mark_price(pred.symbol) or 0,
            'price_lighter': lighter.fetch_mark_price(pred.symbol) or 0
        }
        
        # Determine sides based on prediction
        if pred.predicted_direction == "long_x10":
            opp['leg1_side'] = 'BUY'   # Long on X10
            opp['leg2_side'] = 'SELL'  # Short on Lighter
        else:
            opp['leg1_side'] = 'SELL'  # Short on X10
            opp['leg2_side'] = 'BUY'   # Long on Lighter
        
        opportunities.append(opp)
    
    # Sort by APY * confidence
    opportunities.sort(key=lambda x: x['apy'] * x.get('confidence', 1.0), reverse=True)
    
    logger.info(f"✅ Found {len(opportunities)} opportunities with predictions (farm_mode={is_farm_mode})")
    
    # NUR returnen wenn wir tatsächlich Opportunities haben
    if opportunities:
        return opportunities[:config.MAX_OPEN_TRADES]

    # Wenn keine Predictions, weiter zum Fallback-Code unten
    logger.info("⚠️ Keine Prediction-Opportunities, nutze Fallback mit echten Funding Rates...")

    # Update threshold manager (old code kept for fallback)
    current_rates = [lr for (_s, lr, _xr, _px, _pl) in clean_results if lr is not None]
    if current_rates:
        try:
            threshold_manager.update_metrics(current_rates)
        except Exception as e:
            logger.debug(f"Failed to update threshold metrics: {e}")

    now_ts = time.time()
    valid_pairs = 0
    
    for s, rl, rx, px, pl in clean_results:
        # Debug: Log filter checks
        if s in open_syms:
            logger.debug(f"🔄 Skip {s}: Already open (in open_syms)")
            continue
            
        if s in config.BLACKLIST_SYMBOLS:
            logger.debug(f"⛔ Skip {s}: In BLACKLIST")
            continue
            
        if is_tradfi_or_fx(s):
            logger.debug(f"⛔ Skip {s}: TradFi/FX")
            continue

        if s in FAILED_COINS and (now_ts - FAILED_COINS[s] < 60):
            logger.debug(f"⏰ Skip {s}: Failed cooldown")
            continue

        # Count valid data
        has_rates = rl is not None and rx is not None
        has_prices = px is not None and pl is not None
        
        if has_rates and has_prices:
            valid_pairs += 1
        else:
            continue

        # Update volatility monitor
        if px is not None:
            try:
                get_volatility_monitor().update_price(s, px)
            except Exception as e:
                logger.debug(f"Volatility monitor update failed for {s}: {e}")

        # Price parsing safe check
        try:
            px_float = safe_float(px)
            pl_float = safe_float(pl)
            if px_float <= 0 or pl_float <= 0:
                continue
            spread = abs(px_float - pl_float) / px_float
        except:
            continue

        # ---------------------------------------------------------
        # ⚡ LATENCY ARBITRAGE CHECK
        # ---------------------------------------------------------
        try:
            # Übergebe Adapter, damit Detector Timestamps prüfen kann
            latency_opp = await detector.detect_lag_opportunity(s, rx, rl, x10, lighter)

            if latency_opp:
                # Preise hinzufügen für Execution
                latency_opp['price_x10'] = px_float
                latency_opp['price_lighter'] = pl_float
                latency_opp['spread_pct'] = spread

                logger.info(f"⚡ FAST LANE: Latency Arb gefunden für {s}! Breche Scan ab für sofortige Execution.")
                # Sofortige Rückgabe nur dieser Opportunity, Millisekunden zählen
                return [latency_opp]
        except Exception as e:
            logger.debug(f"Latency Check Error {s}: {e}")

        # ---------------------------------------------------------
        # STANDARD FUNDING STRATEGY
        # ---------------------------------------------------------
        net = rl - rx
        apy = abs(net) * 24 * 365  # Rates sind jetzt Hourly -> 24x am Tag

        # ↓↓↓ DIESE ZEILE EINFÜGEN ↓↓↓
        if apy > 0.5:  # Nur loggen wenn APY > 50%
            logger.info(f"🔍 DEBUG {s}: rl={rl:.8f}, rx={rx:.8f}, net={net:.8f}, apy={apy*100:.1f}%")

        req_apy = threshold_manager.get_threshold(s, is_maker=True)
        if apy < req_apy:
            continue

        if not has_prices:
            logger.debug(f"Skip {s}: Missing prices (X={px}, L={pl})")
            continue
        
        # Spread check
        if spread > config.MAX_SPREAD_FILTER_PERCENT:
            logger.debug(f"Skip {s}: Spread {spread*100:.2f}% too high")
            continue

        # Log high APY opportunities
        if apy > 0.5:
            if now_ts - OPPORTUNITY_LOG_CACHE.get(s, 0) > 60:
                logger.info(f"💎 {s} | APY: {apy*100:.1f}%")
                OPPORTUNITY_LOG_CACHE[s] = now_ts

        opps.append({
            'symbol': s,
            'apy': apy * 100,
            'net_funding_hourly': net,
            'leg1_exchange': 'Lighter' if rl > rx else 'X10',
            'leg1_side': 'SELL' if rl > rx else 'BUY',
            'is_farm_trade': is_farm_mode,  # ✅ DYNAMIC: Set based on mode
            'spread_pct': spread,
            'price_x10': px_float,
            'price_lighter': pl_float,
            'is_latency_arb': False  # Mark als normal funding trade
        })

    # IMPORTANT: Set farm flag based on config (ensure farm loop state applies)
    farm_mode_active = getattr(config, 'VOLUME_FARM_MODE', False)
    for opp in opps:
        if farm_mode_active:
            opp['is_farm_trade'] = True

    # ---------------------------------------------------------
    # SORTIERUNG & DEDUPLIZIERUNG
    # ---------------------------------------------------------
    # Sortieren: Latency Opps haben durch den künstlichen Boost meist Vorrang
    opps.sort(key=lambda x: x['apy'], reverse=True)
    
    # Deduplizieren (falls Symbol durch Latency UND Funding drin ist, nimm Latency)
    unique_opps = {}
    for o in opps:
        sym = o['symbol']
        if sym not in unique_opps:
            unique_opps[sym] = o
        else:
            # Wenn existierendes keine Latency ist, aber neues schon -> überschreiben
            if o.get('is_latency_arb') and not unique_opps[sym].get('is_latency_arb'):
                unique_opps[sym] = o
    
    final_opps = list(unique_opps.values())
    final_opps.sort(key=lambda x: x['apy'], reverse=True)

    logger.info(f"✅ Found {len(final_opps)} opportunities from {valid_pairs} valid pairs (farm_mode={farm_mode_active}, scanned={len(clean_results)})")

    return final_opps[:config.MAX_OPEN_TRADES]

async def execute_trade_task(opp: Dict, lighter, x10, parallel_exec):
    symbol = opp['symbol']
    try:
        await execute_trade_parallel(opp, lighter, x10, parallel_exec)
    except Exception as e:
        logger.error(f"Task {symbol} error: {e}")
    finally:
        # CRITICAL FIX: Always remove from ACTIVE_TASKS
        if symbol in ACTIVE_TASKS:
            try:
                del ACTIVE_TASKS[symbol]
                logger.debug(f"🧹 Cleaned task: {symbol}")
            except KeyError:
                pass

async def launch_trade_task(opp: Dict, lighter, x10, parallel_exec):
    """Launch trade execution in background task."""
    symbol = opp['symbol']
    
    # CRITICAL: Check FAILED_COINS before creating task
    if symbol in FAILED_COINS:
        cooldown = time.time() - FAILED_COINS[symbol]
        if cooldown < 180:  # 3min cooldown
            return
    
    if symbol in ACTIVE_TASKS:
        return

    async def _task_wrapper():
        try:
            await execute_trade_parallel(opp, lighter, x10, parallel_exec)
        except Exception as e:
            logger.error(f"Task {symbol} error: {e}")
        finally:
            # CRITICAL: ALWAYS remove from ACTIVE_TASKS
            if symbol in ACTIVE_TASKS:
                try:
                    del ACTIVE_TASKS[symbol]
                    logger.debug(f"🧹 Cleaned task: {symbol}")
                except KeyError:
                    pass

    task = asyncio.create_task(_task_wrapper())
    ACTIVE_TASKS[symbol] = task

async def execute_trade_parallel(opp: Dict, lighter, x10, parallel_exec) -> bool:
    if SHUTDOWN_FLAG:
        return False

    symbol = opp['symbol']
    reserved_amount = 0.0
    lock = await get_execution_lock(symbol)
    if lock.locked():
        return False

    async with lock:
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Check if position already exists on EITHER exchange
        # This prevents race conditions and duplicate positions
        # ═══════════════════════════════════════════════════════════════
        try:
            x10_positions = await x10.fetch_open_positions()
            lighter_positions = await lighter.fetch_open_positions()
            
            x10_symbols = {p.get('symbol') for p in (x10_positions or [])}
            lighter_symbols = {p.get('symbol') for p in (lighter_positions or [])}
            
            if symbol in x10_symbols:
                logger.warning(f"⚠️ {symbol} already open on X10 - SKIP to prevent duplicate!")
                return False
            
            if symbol in lighter_symbols:
                logger.warning(f"⚠️ {symbol} already open on Lighter - SKIP to prevent duplicate!")
                return False
            
            logger.debug(f"✅ {symbol} verified: No existing positions on exchanges")
            
        except Exception as e:
            logger.error(f"Failed to check existing positions for {symbol}: {e}")
            # Safe default: Skip trade if we can't verify positions
            return False
        
        # Check if already open in state
        existing = await get_open_trades()
        if any(t['symbol'] == symbol for t in existing):
            return False

        # Volatility check
        vol_monitor = get_volatility_monitor()
        if not vol_monitor.can_enter_trade(symbol):
            return False

        # Prediction Logic
        predictor = get_predictor()
        current_rate_lit = lighter.fetch_funding_rate(symbol) or 0.0
        current_rate_x10 = x10.fetch_funding_rate(symbol) or 0.0
        net_rate = current_rate_lit - current_rate_x10

        pred_rate, delta, conf = await predictor.predict_next_funding_rate(
            symbol=symbol,
            current_lighter_rate=current_rate_lit,
            current_x10_rate=current_rate_x10,
            lighter_adapter=lighter,
            x10_adapter=x10,
            btc_price=x10.fetch_mark_price("BTC-USD")
        )

        # Predictor Filter
        if not opp.get('is_farm_trade'):
            if conf > 0.7 and delta < -0.00001:
                logger.info(f"Skipping {symbol}: predictor negative (delta={delta:.6f})")
                return False
            elif conf < 0.3 and abs(net_rate) < 0.0001:
                return False

        # ═══════════════════════════════════════════════════════════════
        # SIZING & VALIDATION MIT KELLY CRITERION
        # ═══════════════════════════════════════════════════════════════
        global IN_FLIGHT_MARGIN, IN_FLIGHT_LOCK

        # 1. Determine Minimum Requirement
        min_req_x10 = x10.min_notional_usd(symbol)
        min_req_lit = lighter.min_notional_usd(symbol)
        min_req = max(min_req_x10, min_req_lit)

        # 2. Check Global Max Constraints
        max_per_trade = float(getattr(config, 'MAX_NOTIONAL_USD', 18.0))  # FIX: Variable war undefiniert
        
        if min_req > config.MAX_TRADE_SIZE_USD:
            logger.debug(f"Skip {symbol}: Min Req ${min_req:.2f} > Max Config ${config.MAX_TRADE_SIZE_USD}")
            return False

        try:
            # 3. Get Balance
            async with IN_FLIGHT_LOCK:
                raw_x10 = await x10.get_real_available_balance()
                raw_lit = await lighter.get_real_available_balance()
                bal_x10_real = max(0.0, raw_x10 - IN_FLIGHT_MARGIN.get('X10', 0.0))
                bal_lit_real = max(0.0, raw_lit - IN_FLIGHT_MARGIN.get('Lighter', 0.0))

            # 4. KELLY CRITERION BERECHNUNG (für ALLE Trades)
            available_capital = min(bal_x10_real, bal_lit_real)
            apy = opp.get('apy', 0.0)
            apy_decimal = apy / 100.0 if apy > 1 else apy  # Normalisiere APY
            
            kelly_sizer = get_kelly_sizer()
            kelly_result = kelly_sizer.calculate_position_size(
                symbol=symbol,
                available_capital=available_capital,
                current_apy=apy_decimal
            )
            
            # KELLY LOGGING (das hat gefehlt!)
            logger.info(
                f"🎰 KELLY {symbol}: win_rate={kelly_result.win_rate:.1%}, "
                f"kelly_fraction={kelly_result.kelly_fraction:.4f}, "
                f"safe_fraction={kelly_result.safe_fraction:.4f}, "
                f"confidence={kelly_result.confidence}, "
                f"samples={kelly_result.sample_size}"
            )

            # 5. Calculate Final Size
            if opp.get('is_farm_trade'):
                target_farm = float(getattr(config, 'FARM_NOTIONAL_USD', 12.0))
                
                # Kelly-Adjustment auch für Farm-Trades
                if kelly_result.safe_fraction > 0.02:
                    # Kelly empfiehlt größere Position
                    kelly_adjusted = min(
                        kelly_result.recommended_size_usd,
                        target_farm * 1.5  # Max 50% über Basis
                    )
                    final_usd = max(target_farm, kelly_adjusted, min_req)
                else:
                    # Kelly ist konservativ - bleibe bei Basis
                    final_usd = max(target_farm, min_req)
                
                logger.info(
                    f"📊 KELLY SIZE {symbol}: base=${target_farm:.1f}, "
                    f"kelly_recommended=${kelly_result.recommended_size_usd:.1f}, "
                    f"final=${final_usd:.1f}"
                )
            else:
                # Non-Farm: Nutze Kelly direkt
                final_usd = kelly_result.recommended_size_usd
                
                if final_usd < min_req:
                    # Upgrade to min_req if within limits AND we have enough balance
                    can_afford = min_req <= available_capital * 0.9  # Max 90% des Kapitals
                    within_limits = min_req <= config.MAX_TRADE_SIZE_USD and min_req <= max_per_trade
                    
                    if can_afford and within_limits:
                        final_usd = float(min_req)
                        logger.info(f"📊 KELLY SIZE {symbol}: upgraded to min_req ${min_req:.1f}")
                    else:
                        reason = "too expensive" if not can_afford else "exceeds limits"
                        logger.debug(f"Skip {symbol}: Size ${final_usd:.2f} < Min ${min_req:.2f} ({reason})")
                        return False
                else:
                    logger.info(f"📊 KELLY SIZE {symbol}: ${final_usd:.1f} (kelly-optimized)")

            # 5. Final Balance Check
            required_x10 = final_usd * 1.05
            required_lit = final_usd * 1.05

            if bal_x10_real < required_x10 or bal_lit_real < required_lit:
                logger.debug(f"Skip {symbol}: Insufficient balance for ${final_usd:.2f}")
                return False

            # 6. Reserve
            async with IN_FLIGHT_LOCK:
                IN_FLIGHT_MARGIN['X10'] = IN_FLIGHT_MARGIN.get('X10', 0.0) + required_x10
                IN_FLIGHT_MARGIN['Lighter'] = IN_FLIGHT_MARGIN.get('Lighter', 0.0) + required_lit
                reserved_amount = final_usd * 1.05 # Track for release

        except Exception as e:
            logger.error(f"Sizing error {symbol}: {e}")
            return False

        # EXECUTE
        try:
            # Determine sides
            leg1_ex = opp.get('leg1_exchange', 'X10')
            leg1_side = opp.get('leg1_side', 'BUY')
            
            x10_side = leg1_side if leg1_ex == 'X10' else ("SELL" if leg1_side == "BUY" else "BUY")
            lit_side = leg1_side if leg1_ex == 'Lighter' else ("SELL" if leg1_side == "BUY" else "BUY")

            logger.info(f"🚀 Opening {symbol}: Size=${final_usd:.1f} (Min=${min_req:.1f})")

            success, x10_id, lit_id = await parallel_exec.execute_trade_parallel(
                symbol=symbol,
                side_x10=x10_side,
                side_lighter=lit_side,
                size_x10=final_usd,
                size_lighter=final_usd
            )

            if success:
                trade_data = {
                    'symbol': symbol,
                    'entry_time': datetime.now(timezone.utc),
                    'notional_usd': final_usd,
                    'status': 'OPEN',
                    'leg1_exchange': leg1_ex,
                    'entry_price_x10': x10.fetch_mark_price(symbol) or 0.0,
                    'entry_price_lighter': lighter.fetch_mark_price(symbol) or 0.0,
                    'is_farm_trade': opp.get('is_farm_trade', False),
                    'account_label': f"Main/Main" # Placeholder until multi-account fix
                }
                await add_trade_to_state(trade_data)
                return True
            else:
                FAILED_COINS[symbol] = time.time()
                return False

        except Exception as e:
            logger.error(f"Execution error {symbol}: {e}")
            FAILED_COINS[symbol] = time.time()
            return False
        finally:
            if reserved_amount > 0:
                async with IN_FLIGHT_LOCK:
                    IN_FLIGHT_MARGIN['X10'] = max(0.0, IN_FLIGHT_MARGIN.get('X10', 0.0) - reserved_amount)
                    IN_FLIGHT_MARGIN['Lighter'] = max(0.0, IN_FLIGHT_MARGIN.get('Lighter', 0.0) - reserved_amount)

async def close_trade(trade: Dict, lighter, x10) -> bool:
    """
    Schließt Trade auf beiden Exchanges.
    KRITISCH: Bei Code-Fehler (TypeError) wird:
    1. X10 emergency-geschlossen
    2. Trade aus State ENTFERNT (um Loop zu verhindern)
    3. Lighter-Orphan zur manuellen Bereinigung geloggt
    """
    symbol = trade['symbol']

    logger.info(f" 🔻 CLOSING {symbol} (Atomic Smart Close)...")

    # ═══════════════════════════════════════════════════════════════
    # SCHRITT 1: Lighter Position schließen
    # ═══════════════════════════════════════════════════════════════
    lighter_success = False
    lighter_code_error = False

    try:
        positions = await lighter.fetch_open_positions()
        pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)

        # PARANOID: Auch hier nochmal casten
        size = safe_float(pos.get('size', 0)) if pos else 0.0

        if pos and abs(size) > 1e-10:
            side = "SELL" if size > 0 else "BUY"
            px = safe_float(lighter.fetch_mark_price(symbol))
            if px > 0:
                usd_size = abs(size) * px
                
                # ═══════════════════════════════════════════════════════════════
                # CRITICAL: Robust try-except around close_live_position
                # Catches TypeError from string/float comparison errors in API limits
                # ═══════════════════════════════════════════════════════════════
                try:
                    result = await lighter.close_live_position(symbol, side, usd_size)
                    lighter_success = result[0] if isinstance(result, tuple) else bool(result)
                except TypeError as type_err:
                    # TypeError indicates data type mismatch (e.g., string vs float comparison)
                    logger.critical(
                        f"🚨 CRITICAL TypeError in Lighter close_live_position for {symbol}: {type_err}\n"
                        f"   This indicates API limit data type mismatch (string vs float).\n"
                        f"   Marking as code error for emergency recovery."
                    )
                    lighter_success = False
                    lighter_code_error = True
                except Exception as close_err:
                    # Other exceptions from close_live_position
                    logger.error(f"❌ Lighter close_live_position failed for {symbol}: {close_err}")
                    lighter_success = False
                    # Don't set lighter_code_error - this is an API error, not a code bug
            else:
                logger.error(f"❌ Lighter close {symbol}: Invalid price {px}")
                lighter_success = False
        else:
            logger.info(f"✅ Lighter {symbol}: No position to close")
            lighter_success = True

    except TypeError as e:
        # TypeError from other parts (e.g., position fetching, data processing)
        logger.critical(f"🚨 CRITICAL TypeError in Lighter close flow for {symbol}: {e}")
        lighter_success = False
        lighter_code_error = True

    except Exception as e:
        logger.error(f"❌ Lighter close failed for {symbol}: {e}")
        lighter_success = False

    # ═══════════════════════════════════════════════════════════════
    # EMERGENCY: Bei Code-Fehler X10 trotzdem schließen UND State bereinigen!
    # ═══════════════════════════════════════════════════════════════
    if lighter_code_error:
        logger.warning(f"⚠️ CODE ERROR!  Force-closing X10 AND cleaning state for {symbol}...")

        # 1.  X10 schließen
        x10_emergency_ok = False
        try:
            await safe_close_x10_position(x10, symbol, "AUTO", 0)
            x10_emergency_ok = True
            logger.info(f"✅ EMERGENCY X10 close for {symbol}")
        except Exception as x10_err:
            logger.error(f"❌ EMERGENCY X10 close FAILED for {symbol}: {x10_err}")

        # 2.  KRITISCH: Trade aus State entfernen, um Loop zu verhindern!
        try:
            await close_trade_in_state(symbol, pnl=0, funding=0)
            logger.info(f"✅ Removed {symbol} from state (preventing loop)")
        except Exception as state_err:
            logger.error(f"❌ Failed to remove {symbol} from state: {state_err}")

        # 3. Telegram-Warnung: Lighter-Orphan manuell prüfen!
        try:
            telegram = get_telegram_bot()
            if telegram and telegram.enabled:
                await telegram.send_error(
                    f"🚨 CODE ERROR RECOVERY: {symbol}\n"
                    f"X10 closed: {'✅' if x10_emergency_ok else '❌'}\n"
                    f"⚠️ Lighter-Position MÖGLICHERWEISE noch offen!\n"
                    f"Manuell prüfen und ggf. schließen!"
                )
        except Exception:
            pass

        # Return True um Loop zu beenden (State wurde bereinigt)
        return x10_emergency_ok

    # ═══════════════════════════════════════════════════════════════
    # NORMALER PFAD: Lighter hat geklappt, jetzt X10
    # ═══════════════════════════════════════════════════════════════
    if not lighter_success:
        # Lighter API-Fehler (nicht Code-Fehler): Nicht X10 schließen!
        logger.error(f"❌ Lighter close FAILED for {symbol}, keeping X10 open as hedge")
        return False

    logger.info(f"✅ Lighter {symbol} OK, closing X10...")

    x10_success = False
    try:
        await safe_close_x10_position(x10, symbol, "AUTO", 0)
        x10_success = True

    except Exception as e:
        logger.error(f"❌ X10 close failed for {symbol}: {e}")
        logger.error(f"⚠️ Lighter ist zu, X10 nicht!  Manuell prüfen!")

        try:
            telegram = get_telegram_bot()
            if telegram and telegram.enabled:
                await telegram.send_error(
                    f"🚨 X10 close failed: {symbol}\n"
                    f"Lighter IST geschlossen!\n"
                    f"Position-Imbalance!  Manuell X10 schließen!"
                )
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════
    # ERGEBNIS & STATE UPDATE
    # ═══════════════════════════════════════════════════════════════
    if lighter_success and x10_success:
        logger.info(f"✅ {symbol} fully closed")
        try:
            await close_trade_in_state(symbol, pnl=0, funding=0)
        except Exception:
            pass
        return True
    else:
        logger.warning(f"⚠️ {symbol} partial: Lighter={lighter_success}, X10={x10_success}")
        # Bei Partial Success: State NICHT bereinigen, damit Retry möglich
        return False

async def get_actual_position_size(adapter, symbol: str) -> Optional[float]:
    """Get actual position size in coins from exchange"""
    try:
        positions = await adapter.fetch_open_positions()
        for p in (positions or []):
            if p.get('symbol') == symbol:
                return safe_float(p.get('size', 0))
        return None
    except Exception as e:
        logger.error(f"Failed to get position size for {symbol}: {e}")
        return None


async def safe_close_x10_position(x10, symbol, side, notional):
    """Close X10 position using ACTUAL position size"""
    try:
        # Wait for settlement
        await asyncio.sleep(3.0)
        
        # Get ACTUAL position
        actual_size = await get_actual_position_size(x10, symbol)
        
        if actual_size is None or abs(actual_size) < 1e-8:
            logger.info(f"✅ No X10 position to close for {symbol}")
            return
        
        actual_size_abs = abs(actual_size)
        
        # Determine side from position
        if actual_size > 0:
            original_side = "BUY"  # LONG
        else:
            original_side = "SELL"  # SHORT
        
        logger.info(f"🔻 Closing X10 {symbol}: size={actual_size_abs:.6f} coins, side={original_side}")
        
        # Close with ACTUAL coin size - safe type conversion for price
        price = safe_float(x10.fetch_mark_price(symbol))
        if price <= 0:
            logger.error(f"No price for {symbol}")
            return
        
        notional_actual = actual_size_abs * price
        
        success, _ = await x10.close_live_position(symbol, original_side, notional_actual)
        
        if success:
            logger.info(f"✅ X10 {symbol} closed ({actual_size_abs:.6f} coins)")
        else:
            logger.error(f"❌ X10 close failed for {symbol}")
    
    except Exception as e:
        logger.error(f"X10 close exception: {e}")

# ============================================================
# LOOPS & MANAGEMENT
# ============================================================
async def get_cached_positions(lighter, x10, force=False):
    """Fetch positions with proper caching and error handling"""
    now = time.time()
    
    # Always force refresh if cache is empty or stale
    cache_age = now - POSITION_CACHE['last_update']
    cache_empty = (len(POSITION_CACHE['x10']) == 0 and len(POSITION_CACHE['lighter']) == 0)
    
    if not force and not cache_empty and cache_age < POSITION_CACHE_TTL:
        logger.debug(f"Using cached positions (age: {cache_age:.1f}s)")
        return POSITION_CACHE['x10'], POSITION_CACHE['lighter']
    
    try:
        logger.debug(f"Fetching fresh positions (force={force}, cache_age={cache_age:.1f}s)")
        
        # Fetch with timeout
        t1 = asyncio.create_task(x10.fetch_open_positions())
        t2 = asyncio.create_task(lighter.fetch_open_positions())
        
        p_x10, p_lit = await asyncio.wait_for(
            asyncio.gather(t1, t2, return_exceptions=True),
            timeout=10.0
        )
        
        # Handle exceptions
        if isinstance(p_x10, Exception):
            logger.error(f"X10 position fetch failed: {p_x10}")
            p_x10 = POSITION_CACHE.get('x10', [])
        
        if isinstance(p_lit, Exception):
            logger.error(f"Lighter position fetch failed: {p_lit}")
            p_lit = POSITION_CACHE.get('lighter', [])
        
        # Ensure lists
        p_x10 = p_x10 if isinstance(p_x10, list) else []
        p_lit = p_lit if isinstance(p_lit, list) else []
        
        # Update cache
        POSITION_CACHE['x10'] = p_x10
        POSITION_CACHE['lighter'] = p_lit
        POSITION_CACHE['last_update'] = now
        
        logger.info(f"📊 Positions: X10={len(p_x10)}, Lighter={len(p_lit)}")
        
        # Debug log actual positions
        if p_x10:
            logger.debug(f"X10 positions: {[p.get('symbol') for p in p_x10]}")
        if p_lit:
            logger.debug(f"Lighter positions: {[p.get('symbol') for p in p_lit]}")
        
        return p_x10, p_lit
        
    except asyncio.TimeoutError:
        logger.error("Position fetch timeout, using stale cache")
        return POSITION_CACHE['x10'], POSITION_CACHE['lighter']
    except Exception as e:
        logger.error(f"Position cache error: {e}")
        import traceback
        traceback.print_exc()
        return POSITION_CACHE.get('x10', []), POSITION_CACHE.get('lighter', [])

async def manage_open_trades(lighter, x10):
    trades = await get_open_trades()
    if not trades: return

    try:
        p_x10, p_lit = await get_cached_positions(lighter, x10)
    except:
        return

    current_time = time.time()

    for t in trades:
        try:
            sym = t['symbol']
            
            # DEBUG: Zeige den Typ und Wert von entry_time
            raw_entry_time = t.get('entry_time')
            logger.info(f"🔍 DEBUG {sym}: entry_time type={type(raw_entry_time)}, value={raw_entry_time}")
            
            # ═══════════════════════════════════════════════════════════════
            # FIX: Daten-Sanitizing für ALLE numerischen Felder
            # ═══════════════════════════════════════════════════════════════
            
            # 1. Notional sanitizen
            notional = t.get('notional_usd')
            notional = float(notional) if notional is not None else 0.0

            # 2. Initial Funding sanitizen
            init_funding = t.get('initial_funding_rate_hourly')
            init_funding = float(init_funding) if init_funding is not None else 0.0
            
            # 3. Entry Time sanitizen und Age berechnen
            entry_time = t.get('entry_time')
            
            # Fix: entry_time kann als String aus JSON/DB kommen
            if entry_time:
                if isinstance(entry_time, str):
                    # Parse ISO format string zurück zu datetime
                    entry_time = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
                
                # Stelle sicher, dass entry_time timezone-aware ist
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                
                age_seconds = (datetime.now(timezone.utc) - entry_time).total_seconds()
            else:
                # Fallback: Wenn keine entry_time, nutze created_at oder setze hohes Alter
                created_at = t.get('created_at')
                if created_at:
                    if isinstance(created_at, str):
                        created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
                else:
                    # Kein Zeitstempel vorhanden - Force Close nach 60s annehmen
                    logger.warning(f"⚠️ Kein entry_time für {t.get('symbol')} - setze age auf FARM_HOLD_SECONDS+1")
                    age_seconds = config.FARM_HOLD_SECONDS + 1
            
            # DEBUG LOG (optional, um zu sehen was passiert)
            logger.debug(f"Check {sym}: Age={age_seconds:.1f}s (Limit={config.FARM_HOLD_SECONDS}s)")
            
            # ═══════════════════════════════════════════════════════════════
            # NOT-AUS: Force Close nach Zeit (VOR Preis-Check!)
            # ═══════════════════════════════════════════════════════════════
            # KRITISCH: Prüfe Zeit-Limit ZUERST, bevor wir Preise brauchen.
            # Das garantiert, dass der Bot auch bei schlechter Verbindung schließt.
            
            if config.VOLUME_FARM_MODE and t.get('is_farm_trade') and age_seconds > config.FARM_HOLD_SECONDS:
                logger.warning(
                    f"🚜 [FARM] NOT-AUS: Zwangsschließung für {sym} "
                    f"(Zeit abgelaufen: {int(age_seconds)}s > {config.FARM_HOLD_SECONDS}s)"
                )
                
                # Versuche zu schließen, AUCH WENN wir keine Preise haben
                if await close_trade(t, lighter, x10):
                    await close_trade_in_state(sym)
                    await archive_trade_to_history(t, "FARM_TIME_LIMIT", {
                        'total_net_pnl': 0.0,  # Unbekannt ohne Preise
                        'funding_pnl': 0.0, 
                        'spread_pnl': 0.0, 
                        'fees': notional * 0.0005  # Geschätzte Fees
                    })
                    logger.info(f"✅ {sym} zwangsgeschlossen (Farm Time Limit)")
                else:
                    logger.error(f"❌ Zwangsschließung von {sym} fehlgeschlagen - Retry im nächsten Cycle")
                
                continue  # Nächster Trade
            
            # ═══════════════════════════════════════════════════════════════
            # Preise holen (für Profit-Check & normale Exits)
            # ═══════════════════════════════════════════════════════════════
            raw_px = x10.fetch_mark_price(sym)
            raw_pl = lighter.fetch_mark_price(sym)
            px = safe_float(raw_px) if raw_px is not None else None
            pl = safe_float(raw_pl) if raw_pl is not None else None

            # REST Fallback wenn WebSocket keine Preise hat
            if px is None or pl is None or px <= 0 or pl <= 0:
                logger.debug(f"{sym}: WS Preise fehlen, versuche REST Fallback...")
                
                # Versuche REST API als Fallback
                try:
                    if px is None or px <= 0:
                        # X10 REST Fallback (falls Methode existiert)
                        if hasattr(x10, 'get_price_rest'):
                            px = safe_float(await x10.get_price_rest(sym))
                        elif hasattr(x10, 'load_market_cache'):
                            await x10.load_market_cache(force=True)
                            px = safe_float(x10.fetch_mark_price(sym))
                    
                    if pl is None or pl <= 0:
                        # Lighter REST Fallback
                        if hasattr(lighter, 'get_price_rest'):
                            pl = safe_float(await lighter.get_price_rest(sym))
                        elif hasattr(lighter, 'load_funding_rates_and_prices'):
                            await lighter.load_funding_rates_and_prices()
                            pl = safe_float(lighter.fetch_mark_price(sym))
                    
                    if px is not None and pl is not None and px > 0 and pl > 0:
                        logger.info(f"✅ {sym}: REST Fallback erfolgreich (X10=${px:.2f}, Lit=${pl:.2f})")
                except Exception as e:
                    logger.warning(f"{sym}: REST Fallback fehlgeschlagen: {e}")
                
                # Wenn immer noch keine Preise -> Skip (aber Zeit-Check wurde oben schon gemacht!)
                if px is None or pl is None or px <= 0 or pl <= 0:
                    logger.debug(f"{sym}: Keine Preise verfügbar, überspringe Profit-Check")
                    continue

            rx = x10.fetch_funding_rate(sym) or 0.0
            rl = lighter.fetch_funding_rate(sym) or 0.0
            
            # Net Calc
            base_net = rl - rx
            current_net = -base_net if t.get('leg1_exchange') == 'X10' else base_net

            # PnL & Duration (nutze bereits berechnetes age_seconds)
            hold_hours = age_seconds / 3600
            funding_pnl = current_net * hold_hours * notional

            # Spread PnL
            ep_x10 = float(t.get('entry_price_x10') or px) 
            ep_lit = float(t.get('entry_price_lighter') or pl)
            
            entry_spread = abs(ep_x10 - ep_lit)
            curr_spread = abs(px - pl)
            
            if px > 0:
                spread_pnl = (entry_spread - curr_spread) / px * notional
                current_spread_pct = curr_spread / px
            else:
                spread_pnl = 0.0
                current_spread_pct = 0.0

            # Fees
            fee_x10 = getattr(config, 'TAKER_FEE_X10', 0.0006)
            fee_lit = 0.0  # Lighter hat keine/niedrige Taker Fees
            est_fees = notional * (fee_x10 + fee_lit) * 2.0

            total_pnl = funding_pnl + spread_pnl - est_fees

            # ═══════════════════════════════════════════════════════════════
            # FARM MODE PROFIT-MITNAHME (Nur wenn Preise verfügbar!)
            # ═══════════════════════════════════════════════════════════════
            reason = None
            
            # 1. FARM MODE: Profit Take bei günstigem Spread
            if not reason and t.get('is_farm_trade') and config.VOLUME_FARM_MODE:
                # ÄNDERUNG: Statt 300s hartcodiert, nehmen wir einen kürzeren Wert
                min_hold_time = getattr(config, 'FARM_MIN_HOLD_SECONDS', 15)  # Standard: 15 Sekunden
                
                # Erst nach min_hold_time prüfen
                if age_seconds > min_hold_time:
                    # Bei deinen Fees (0.045% roundtrip) ist alles unter 0.04% Spread Profit
                    # Spread < 0.04% = günstige Exit-Bedingung
                    if current_spread_pct < 0.0004:  # 0.04%
                        reason = "FARM_QUICK_PROFIT"
                        logger.info(
                            f"🚜 [FARM] Profit Take {sym}: Age={int(age_seconds)}s, "
                            f"Spread={current_spread_pct*100:.3f}% (sehr niedrig), PnL=${total_pnl:.2f}"
                        )
                    # Alternative: Wenn Total PnL positiv und Spread akzeptabel
                    elif total_pnl > 0 and current_spread_pct < 0.0005:  # 0.05%
                        reason = "FARM_QUICK_PROFIT"
                        logger.info(
                            f"🚜 [FARM] Profit Take {sym}: Age={int(age_seconds)}s, "
                            f"PnL=${total_pnl:.2f}, Spread={current_spread_pct*100:.3f}%"
                        )

            # 2. Volatility Panic
            if not reason:
                try:
                    vol_mon = get_volatility_monitor()
                    vol = 0.0
                    if hasattr(vol_mon, 'get_volatility'):
                        val = vol_mon.get_volatility(sym)
                        if val is not None:
                            vol = float(val)
                    
                    if hasattr(vol_mon, 'should_close_due_to'):
                        if vol_mon.should_close_due_to(vol):
                            reason = "VOLATILITY_PANIC"
                except Exception as e:
                    logger.warning(f"VolCheck Warning {sym}: {e}")

            # 3. Stop Loss / Take Profit
            if not reason:
                if notional > 0:
                    if total_pnl < -notional * 0.03:
                        reason = "STOP_LOSS"
                    elif total_pnl > notional * 0.05:
                        reason = "TAKE_PROFIT"
                
                # Funding Flip Check (Nutze jetzt das sichere init_funding)
                if init_funding * current_net < 0:
                    if not t.get('funding_flip_start_time'):
                        t['funding_flip_start_time'] = datetime.utcnow()
                        if state_manager:
                            await state_manager.update_trade(sym, {'funding_flip_start_time': datetime.utcnow()})
                    else:
                        flip_start = t['funding_flip_start_time']
                        if isinstance(flip_start, str): 
                            try: flip_start = datetime.fromisoformat(flip_start)
                            except: flip_start = datetime.utcnow()
                        
                        if (datetime.utcnow() - flip_start).total_seconds() / 3600 > config.FUNDING_FLIP_HOURS_THRESHOLD:
                            reason = "FUNDING_FLIP"
                else:
                    if t.get('funding_flip_start_time'):
                        if state_manager:
                            await state_manager.update_trade(sym, {'funding_flip_start_time': None})

            if reason:
                logger.info(f" 💸 EXIT {sym}: {reason} | PnL ${total_pnl:.2f} (Fees: ${est_fees:.2f})")
                if await close_trade(t, lighter, x10):
                    await close_trade_in_state(sym)
                    await archive_trade_to_history(t, reason, {
                        'total_net_pnl': total_pnl, 'funding_pnl': funding_pnl,
                        'spread_pnl': spread_pnl, 'fees': est_fees
                    })
                    # Kelly Sizer Trade Recording
                    try:
                        kelly_sizer = get_kelly_sizer()
                        kelly_sizer.record_trade(
                            symbol=sym,
                            pnl_usd=total_pnl,
                            hold_time_seconds=hold_hours * 3600,
                            entry_apy=t.get('apy', None)
                        )
                    except Exception as e:
                        logger.error(f"Kelly Sizer record_trade error for {sym}: {e}")
                    telegram = get_telegram_bot()
                    if telegram.enabled:
                        await telegram.send_trade_alert(sym, reason, notional, total_pnl)

        except Exception as e:
            logger.error(f"Trade Loop Error for {t.get('symbol', 'UNKNOWN')}: {e}")
            import traceback
            logger.debug(traceback.format_exc())

async def sync_check_and_fix(lighter, x10):
    """
    Prüft ob X10 und Lighter Positionen synchron sind und fixt Differenzen.
    CRITICAL: Verhindert ungehedgte Positionen die zu Directional Risk führen.
    Sollte beim Start und alle 5-10 Minuten laufen.
    """
    logger.info("🔍 Starting Exchange Sync Check...")
    
    try:
        # Fetch actual positions from both exchanges
        x10_positions = await x10.fetch_open_positions()
        lighter_positions = await lighter.fetch_open_positions()
        
        # Extract symbols (filter out dust positions)
        x10_symbols = {
            p.get('symbol') for p in (x10_positions or [])
            if abs(safe_float(p.get('size', 0))) > 1e-8
        }
        lighter_symbols = {
            p.get('symbol') for p in (lighter_positions or [])
            if abs(safe_float(p.get('size', 0))) > 1e-8
        }
        
        # Find desync
        only_on_x10 = x10_symbols - lighter_symbols
        only_on_lighter = lighter_symbols - x10_symbols
        
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Orphaned X10 positions (no Lighter hedge)
        # ═══════════════════════════════════════════════════════════════
        if only_on_x10:
            logger.error(f"🚨 DESYNC DETECTED: Positions only on X10: {only_on_x10}")
            logger.error(f"⚠️ These are UNHEDGED and create directional risk!")
            
            # Optional: Send Telegram alert
            try:
                telegram = get_telegram_bot()
                if telegram and telegram.enabled:
                    await telegram.send_error(
                        f"🚨 EXCHANGE DESYNC!\n"
                        f"Orphaned X10 positions: {only_on_x10}\n"
                        f"Closing to prevent directional risk..."
                    )
            except Exception:
                pass
            
            for sym in only_on_x10:
                try:
                    logger.warning(f"🔻 Closing orphaned X10 position: {sym}")
                    pos = next((p for p in x10_positions if p.get('symbol') == sym), None)
                    if pos:
                        size = safe_float(pos.get('size', 0))
                        original_side = "BUY" if size > 0 else "SELL"
                        px = safe_float(x10.fetch_mark_price(sym))
                        if px > 0:
                            notional = abs(size) * px
                            # CRITICAL: Robust try-except for TypeError from API limit comparisons
                            try:
                                await x10.close_live_position(sym, original_side, notional)
                                logger.info(f"✅ Closed orphaned X10 {sym}")
                            except TypeError as type_err:
                                logger.critical(
                                    f"🚨 CRITICAL TypeError in X10 close for orphan {sym}: {type_err}\n"
                                    f"   This indicates API limit data type mismatch (string vs float).\n"
                                    f"   Position may still be open - manual intervention required!"
                                )
                                try:
                                    telegram = get_telegram_bot()
                                    if telegram and telegram.enabled:
                                        await telegram.send_error(
                                            f"🚨 TypeError closing X10 orphan {sym}!\n"
                                            f"Error: {type_err}\n"
                                            f"Manual close required!"
                                        )
                                except Exception:
                                    pass
                except Exception as e:
                    logger.error(f"Failed to close X10 orphan {sym}: {e}")
        
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Orphaned Lighter positions (no X10 hedge)
        # ═══════════════════════════════════════════════════════════════
        if only_on_lighter:
            # Filtere Symbole heraus, die gerade gehandelt werden
            real_orphans = []
            
            for sym in only_on_lighter:
                # HOL DIR DEN LOCK: Wenn er gelockt ist, läuft gerade ein Trade!
                lock = await get_execution_lock(sym)
                if lock.locked():
                    logger.info(f"🔒 Skipping sync check for {sym} (Execution in progress)")
                    continue
                
                # Checke auch ACTIVE_TASKS zur Sicherheit
                if sym in ACTIVE_TASKS:
                    logger.info(f"🔒 Skipping sync check for {sym} (Task Active)")
                    continue
                    
                real_orphans.append(sym)

            if real_orphans:
                logger.error(f"🚨 DESYNC DETECTED: Positions only on Lighter: {real_orphans}")
                logger.error(f"⚠️ These are UNHEDGED and create directional risk!")
                
                # Optional: Send Telegram alert
                try:
                    telegram = get_telegram_bot()
                    if telegram and telegram.enabled:
                        await telegram.send_error(
                            f"🚨 EXCHANGE DESYNC!\n"
                            f"Orphaned Lighter positions: {real_orphans}\n"
                            f"Closing to prevent directional risk..."
                        )
                except Exception:
                    pass
                
                for sym in real_orphans:
                    try:
                        logger.warning(f"🔻 Closing orphaned Lighter position: {sym}")
                        pos = next((p for p in lighter_positions if p.get('symbol') == sym), None)
                        if pos:
                            size = safe_float(pos.get('size', 0))
                            original_side = "BUY" if size > 0 else "SELL"
                            px = safe_float(lighter.fetch_mark_price(sym))
                            if px > 0:
                                notional = abs(size) * px
                                # CRITICAL: Robust try-except for TypeError
                                try:
                                    await lighter.close_live_position(sym, original_side, notional)
                                    logger.info(f"✅ Closed orphaned Lighter {sym}")
                                except TypeError as type_err:
                                    logger.critical(f"🚨 TypeError closing Lighter orphan {sym}: {type_err}")
                    except Exception as e:
                        logger.error(f"Failed to close Lighter orphan {sym}: {e}")
        
        # ═══════════════════════════════════════════════════════════════
        # SUCCESS: Exchanges are in sync
        # ═══════════════════════════════════════════════════════════════
        if not only_on_x10 and not only_on_lighter:
            common = x10_symbols & lighter_symbols
            logger.info(f"✅ Exchanges are SYNCED: {len(common)} paired positions")
            if common:
                logger.debug(f"   Synced symbols: {common}")
        
    except Exception as e:
        logger.error(f"Sync check failed: {e}")
        import traceback
        logger.debug(traceback.format_exc())

async def cleanup_zombie_positions(lighter, x10):
    """Full Zombie Cleanup Implementation with Correct Side Detection"""
    try:
        x_pos, l_pos = await get_cached_positions(lighter, x10, force=True)

        x_syms = {p['symbol'] for p in x_pos if abs(safe_float(p.get('size', 0))) > 1e-8}
        l_syms = {p['symbol'] for p in l_pos if abs(safe_float(p.get('size', 0))) > 1e-8}

        db_trades = await get_open_trades()
        db_syms = {t['symbol'] for t in db_trades}

        # 1. Exchange Zombies (Open on Exchange, Closed in DB)
        all_exchange = x_syms | l_syms
        zombies = all_exchange - db_syms

        if zombies:
            logger.warning(f"🧟 ZOMBIES DETECTED: {zombies}")
            for sym in zombies:
                # X10 Zombie Cleanup
                if sym in x_syms:
                    p = next((pos for pos in x_pos if pos['symbol'] == sym), None)
                    if p:
                        position_size = safe_float(p.get('size', 0))

                        # CRITICAL FIX: Determine close side based on CURRENT position
                        # Positive size = LONG position → close with SELL
                        # Negative size = SHORT position → close with BUY
                        if position_size > 0:
                            close_side = "SELL"  # Close LONG
                            original_side = "BUY"
                        else:
                            close_side = "BUY"   # Close SHORT
                            original_side = "SELL"

                        size_usd = abs(position_size) * safe_float(x10.fetch_mark_price(sym))

                        if size_usd < 1.0:
                            logger.warning(f"⚠️ X10 zombie {sym} too small (${size_usd:.2f}), skipping")
                            continue

                        logger.info(f"🔻 Closing X10 zombie {sym}: pos_size={position_size:.6f}, close={close_side}, ${size_usd:.1f}")

                        # Use close_live_position with ORIGINAL side (it handles inversion internally)
                        try:
                            success, _ = await x10.close_live_position(sym, original_side, size_usd)
                            if not success:
                                logger.error(f"❌ Failed to close X10 zombie {sym}")
                            else:
                                logger.info(f"✅ Closed X10 zombie {sym}")
                        except TypeError as type_err:
                            logger.critical(
                                f"🚨 TypeError in X10 zombie close for {sym}: {type_err}\n"
                                f"   API limit data type mismatch - manual close required!"
                            )
                        except Exception as e:
                            logger.error(f"X10 zombie close exception for {sym}: {e}")

                # Lighter Zombie Cleanup
                if sym in l_syms:
                    p = next((pos for pos in l_pos if pos['symbol'] == sym), None)
                    if p:
                        position_size = safe_float(p.get('size', 0))

                        # CRITICAL FIX: Same logic for Lighter
                        if position_size > 0:
                            close_side = "SELL"
                            original_side = "BUY"
                        else:
                            close_side = "BUY"
                            original_side = "SELL"

                        size_usd = abs(position_size) * safe_float(lighter.fetch_mark_price(sym))

                        if size_usd < 1.0:
                            logger.warning(f"⚠️ Lighter zombie {sym} too small (${size_usd:.2f}), skipping")
                            continue

                        logger.info(f"🔻 Closing Lighter zombie {sym}: pos_size={position_size:.6f}, close={close_side}, ${size_usd:.1f}")

                        try:
                            success, _ = await lighter.close_live_position(sym, original_side, size_usd)
                            if not success:
                                logger.error(f"❌ Failed to close Lighter zombie {sym}")
                            else:
                                logger.info(f"✅ Closed Lighter zombie {sym}")
                        except TypeError as type_err:
                            logger.critical(
                                f"🚨 TypeError in Lighter zombie close for {sym}: {type_err}\n"
                                f"   API limit data type mismatch - manual close required!"
                            )
                        except Exception as e:
                            logger.error(f"Lighter zombie close exception for {sym}: {e}")

        # 2. DB Ghosts (Open in DB, Closed on Exchange)
        ghosts = db_syms - all_exchange
        if ghosts:
            # ✅ NEW: Filter out recent trades (< 30s old) to avoid false positives
            real_ghosts = set()
            for sym in ghosts:
                trade = next((t for t in db_trades if t['symbol'] == sym), None)
                if not trade:
                    continue

                entry_time = trade.get('entry_time')
                if isinstance(entry_time, str):
                    try:
                        entry_time = datetime.fromisoformat(entry_time)
                    except:
                        entry_time = datetime.utcnow()
                elif not isinstance(entry_time, datetime):
                    entry_time = datetime.utcnow()

                age_seconds = (datetime.utcnow() - entry_time).total_seconds()

                # Only treat as ghost if trade is older than 30s
                if age_seconds > 30:
                    real_ghosts.add(sym)
                else:
                    logger.debug(f"👻 {sym} is new trade ({age_seconds:.1f}s old), not a ghost")

            if real_ghosts:
                logger.warning(f"👻 REAL GHOSTS DETECTED: {real_ghosts}")
                for sym in real_ghosts:
                    logger.info(f"Closing ghost {sym} in DB")
                    try:
                        await close_trade_in_state(sym)
                    except Exception as e:
                        logger.error(f"Failed to close ghost {sym} in DB: {e}")

    except Exception as e:
        logger.error(f"Zombie Cleanup Error: {e}")


async def balance_watchdog():
    """KOMPLETT DEAKTIVIERT – X10 Starknet Migration macht Balance-Erkennung unzuverlässig"""
    return  # ← Sofort returnen, keine Logik


async def emergency_position_cleanup(lighter, x10, max_age_hours: float = 48.0, interval_seconds: int = 3600):
    """Emergency background task to perform periodic zombie/ghost cleanup.

    This is a defensive fallback if no external emergency cleanup routine
    is provided elsewhere in the codebase. It simply runs
    `cleanup_zombie_positions` on an interval and logs any exceptions.
    """
    logger.info(f"Starting emergency_position_cleanup (interval={interval_seconds}s, max_age_hours={max_age_hours})")
    try:
        while not SHUTDOWN_FLAG:
            try:
                await cleanup_zombie_positions(lighter, x10)
            except Exception as e:
                logger.error(f"Emergency cleanup iteration failed: {e}")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("emergency_position_cleanup cancelled")
    except Exception as e:
        logger.error(f"emergency_position_cleanup error: {e}")

async def farm_loop(lighter, x10, parallel_exec):
    """Volume Farming"""
    # Rate limiter state (local to function to avoid global mutation)
    last_trades: List[float] = []

    # Keep this coroutine alive even when farming is disabled.
    # Return would terminate the background task and leave the bot without the farm loop.
    while True:
        if SHUTDOWN_FLAG:
            logger.info("Farm loop: SHUTDOWN_FLAG detected, exiting")
            break
        try:
            # If farming disabled, sleep and continue — don't exit the task
            if not config.VOLUME_FARM_MODE:
                await asyncio.sleep(60)
                continue

            # Announce when farm mode is active (logged each loop iteration when active)
            logger.info("🚜 Farm Mode ACTIVE")
            if SHUTDOWN_FLAG:
                break
            
            # BLOCKER: Wenn Arbitrage gerade aktiv war (letzte 10s), pausiere Farming
            # Das verhindert, dass Farm Slots klaut, während Arb noch executed
            if time.time() - LAST_ARBITRAGE_LAUNCH < 10.0:
                await asyncio.sleep(1)
                continue
            
            # Clean rate limiter history
            now = time.time()
            last_trades = [t for t in last_trades if now - t < 60]
            
            # Check burst limit (safe default)
            burst_limit = getattr(config, 'FARM_BURST_LIMIT', 10)
            if len(last_trades) >= burst_limit:
                oldest = last_trades[0]
                wait_time = 60 - (now - oldest)
                logger.info(f"🚜 Rate limited: wait {wait_time:.1f}s")
                await asyncio.sleep(max(wait_time, 5))
                continue
            
            # Standard farm logic
            trades = await get_open_trades()
            farm_count = sum(1 for t in trades if t.get('is_farm_trade'))

            # FIX: Deduplizierung von DB-Trades und aktiven Tasks
            open_symbols_db = {t['symbol'] for t in trades}
            
            async with TASKS_LOCK:
                executing_symbols = set(ACTIVE_TASKS.keys())
            
            # Bilde die Vereinigungsmenge (Union) -> Jedes Symbol zählt nur 1x
            all_active_symbols = open_symbols_db | executing_symbols
            current_total_count = len(all_active_symbols)
            
            # Globales Limit prüfen
            total_limit = getattr(config, 'MAX_OPEN_TRADES', 3)
            
            if current_total_count >= total_limit:
                await asyncio.sleep(5)
                continue

            # Check concurrency limit (Farm spezifisch)
            if farm_count >= getattr(config, 'FARM_MAX_CONCURRENT', 3):
                await asyncio.sleep(10)
                continue

            # Check min interval since last trade
            min_interval = getattr(config, 'FARM_MIN_INTERVAL_SECONDS', 15)
            if last_trades:
                last_trade = last_trades[-1]
                time_since = now - last_trade
                if time_since < min_interval:
                    await asyncio.sleep(max(0.0, min_interval - time_since))

            # Get balance
            bal_x10 = await x10.get_real_available_balance()
            bal_lit = await lighter.get_real_available_balance()
            
            if bal_x10 < getattr(config, 'FARM_NOTIONAL_USD', 0) or bal_lit < getattr(config, 'FARM_NOTIONAL_USD', 0):
                logger.debug(f"🚜 Farm paused: Low balance X10=${bal_x10:.0f} Lit=${bal_lit:.0f}")
                await asyncio.sleep(30)
                continue

            # Find farm opportunity
            common = set(lighter.market_info.keys()) & set(x10.market_info.keys())
            open_syms = {t['symbol'] for t in trades}
            
            for sym in common:
                # Prevent spam / re-triggers: skip if open, already executing, or recently failed
                if sym in open_syms or sym in ACTIVE_TASKS or sym in FAILED_COINS:
                    continue
                
                px = safe_float(x10.fetch_mark_price(sym))
                pl = safe_float(lighter.fetch_mark_price(sym))
                
                if px <= 0 or pl <= 0:
                    continue
                
                spread = abs(px - pl) / px if px > 0 else 1.0
                if spread > getattr(config, 'FARM_MAX_SPREAD_PCT', 0.01):
                    continue
                
                vol_24h = abs(x10.get_24h_change_pct(sym) or 0)
                if vol_24h > getattr(config, 'FARM_MAX_VOLATILITY_24H', 0.05):
                    continue
                
                rl = lighter.fetch_funding_rate(sym) or 0
                rx = x10.fetch_funding_rate(sym) or 0
                apy = abs(rl - rx) * 24 * 365
                
                if apy < getattr(config, 'FARM_MIN_APY', 0.01):
                    continue
                
                # CRITICAL: Mark as farm trade
                opp = {
                    'symbol': sym,
                    'apy': apy * 100,
                    'net_funding_hourly': rl - rx,
                    'leg1_exchange': 'Lighter' if rl > rx else 'X10',
                    'leg1_side': 'SELL' if rl > rx else 'BUY',
                    'is_farm_trade': True,
                    'spread_pct': spread
                }
                
                logger.info(f"🚜 Opening FARM: {sym} APY={apy*100:.1f}%")
                
                ACTIVE_TASKS[sym] = asyncio.create_task(
                    execute_trade_task(opp, lighter, x10, parallel_exec)
                )
                
                # Record this trade attempt in local rate limiter
                last_trades.append(now)

                await asyncio.sleep(10)  # Wait longer between farm trades
                break
            
            await asyncio.sleep(30)  # Longer idle time between scans
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌ Farm Error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(30)  # Long cooldown on error

async def connection_watchdog(ws_manager=None, x10=None, lighter=None):
    """Watchdog zur Erkennung von Connection-Timeouts"""
    global LAST_DATA_UPDATE, SHUTDOWN_FLAG
    
    logger.info("🐕 Connection Watchdog gestartet (Timeout: 120s)")
    
    while not SHUTDOWN_FLAG:
        try:
            await asyncio.sleep(30)  # Alle 30s prüfen
            
            if SHUTDOWN_FLAG:
                break
            
            now = time.time()
            time_since_update = now - LAST_DATA_UPDATE
            
            if time_since_update > WATCHDOG_TIMEOUT:
                logger.warning(
                    f"⚠️ WATCHDOG ALARM: Keine Daten seit {time_since_update:.0f}s! "
                    f"(Limit: {WATCHDOG_TIMEOUT}s)"
                )
                
                # Telegram Alert
                telegram = get_telegram_bot()
                if telegram and telegram.enabled:
                    await telegram.send_error(
                        f"🐕 Connection Watchdog Alert!\n"
                        f"Keine Daten seit {time_since_update:.0f}s\n"
                        f"Versuche Reconnect..."
                    )
                
                # Versuche Reconnect
                if ws_manager:
                    try:
                        logger.info("🔄 Watchdog initiiert WebSocket Reconnect...")
                        await ws_manager.reconnect_all()
                        LAST_DATA_UPDATE = time.time()  # Reset nach Reconnect
                        logger.info("✅ Watchdog Reconnect erfolgreich")
                    except Exception as e:
                        logger.error(f"❌ Watchdog Reconnect fehlgeschlagen: {e}")
                
                # Fallback: Force-Refresh via REST
                if x10 and lighter:
                    try:
                        logger.info("🔄 Watchdog: Force-Refresh Market Data via REST...")
                        await asyncio.gather(
                            x10.load_market_cache(force=True),
                            lighter.load_market_cache(force=True),
                            return_exceptions=True
                        )
                        LAST_DATA_UPDATE = time.time()
                        logger.info("✅ Watchdog REST Refresh erfolgreich")
                    except Exception as e:
                        logger.error(f"❌ Watchdog REST Refresh fehlgeschlagen: {e}")
                
                # Wenn nach 2 Minuten immer noch tot → System Exit
                # (Supervisor/Docker kann Bot dann neu starten)
                if time_since_update > WATCHDOG_TIMEOUT * 2:
                    logger.critical(
                        f"🚨 CRITICAL: Keine Daten seit {time_since_update:.0f}s! "
                        "Bot wird beendet (Supervisor startet neu)..."
                    )
                    if telegram and telegram.enabled:
                        await telegram.send_error(
                            "🚨 Bot CRITICAL ERROR - Neustart erforderlich"
                        )
                    # Graceful Exit für Supervisor Restart
                    SHUTDOWN_FLAG = True
                    break
            
            else:
                # Alles OK
                logger.debug(f"🐕 Watchdog OK: Daten-Alter {time_since_update:.1f}s")
        
        except asyncio.CancelledError:
            logger.info("Connection watchdog cancelled")
            break
        except Exception as e:
            logger.error(f"Connection watchdog error: {e}")
            await asyncio.sleep(30)

async def cleanup_finished_tasks():
    """Background task: Remove completed tasks from ACTIVE_TASKS"""
    while not SHUTDOWN_FLAG:
        try:
            async with TASKS_LOCK:
                finished = [sym for sym, task in ACTIVE_TASKS.items() if task.done()]
                for sym in finished:
                    try:
                        del ACTIVE_TASKS[sym]
                    except KeyError:
                        pass
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"cleanup_finished_tasks error: {e}")

    async def emergency_position_cleanup(lighter, x10, max_age_hours: float = 48.0):
        """
        Emergency cleanup für stuck trades die länger als max_age_hours offen sind.
        Wird alle 10 Minuten aufgerufen.
        """
        while not SHUTDOWN_FLAG:
            try:
                await asyncio.sleep(600)  # 10 min
            
                if SHUTDOWN_FLAG:
                    break
                
                open_trades = await get_open_trades()
                now = datetime.now()
            
                for trade in open_trades:
                    entry_time = trade.get('entry_time')
                    if not entry_time:
                        continue
                    
                    if isinstance(entry_time, str):
                        entry_time = datetime.fromisoformat(entry_time)
                
                    age_hours = (now - entry_time).total_seconds() / 3600
                
                    if age_hours > max_age_hours:
                        symbol = trade['symbol']
                        logger.warning(
                            f"🚨 EMERGENCY CLOSE: {symbol} open for {age_hours:.1f}h (max={max_age_hours}h)"
                        )
                    
                        # Force close via position_manager
                        from src.position_manager import close_position_with_reason
                        await close_position_with_reason(
                            symbol, 
                            "EMERGENCY_TIMEOUT", 
                            lighter, 
                            x10
                        )
                        await asyncio.sleep(2)
                    
            except asyncio.CancelledError:
                logger.info("Emergency cleanup cancelled")
                break
            except Exception as e:
                logger.error(f"Emergency cleanup error: {e}")
                await asyncio.sleep(60)

async def close_all_open_positions_on_start(lighter, x10):
    """
    EMERGENCY: Close all open positions on bot start.
    Use this if bot is stuck with 20 open trades blocking capital.
    """
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

def get_symbol_lock(symbol: str) -> asyncio.Lock:
    """Thread-safe / async-safe Lock pro Symbol"""
    if symbol not in SYMBOL_LOCKS:
        SYMBOL_LOCKS[symbol] = asyncio.Lock()
    return SYMBOL_LOCKS[symbol]


async def reconcile_db_with_exchanges(lighter, x10):
    """
    CRITICAL: Reconcile database state with actual exchange positions.
    Closes DB entries for trades that don't exist on exchanges.
    """
    logger.info("🔍 STATE RECONCILIATION: Checking DB vs Exchange...")
    
    # Get DB trades
    open_trades = await get_open_trades()
    if not open_trades:
        logger.info("✓ No DB trades to reconcile")
        return
    
    # Get actual exchange positions
    try:
        x10_positions = await x10.fetch_open_positions()
        lighter_positions = await lighter.fetch_open_positions()
    except Exception as e:
        logger.error(f"✗ Failed to fetch positions: {e}")
        return
    
    x10_symbols = {p.get('symbol') for p in (x10_positions or [])}
    lighter_symbols = {p.get('symbol') for p in (lighter_positions or [])}
    
    # Find ghost trades (in DB but not on exchanges)
    ghost_count = 0
    for trade in open_trades:
        symbol = trade['symbol']
        
        # Trade should exist on both exchanges
        on_x10 = symbol in x10_symbols
        on_lighter = symbol in lighter_symbols
        
        if not on_x10 and not on_lighter:
            # Ghost trade - exists in DB but not on exchanges
            logger.warning(
                f"👻 GHOST TRADE: {symbol} in DB but NOT on exchanges - cleaning DB"
            )
            
            # Mark as closed in DB (use existing wrapper)
            try:
                await close_trade_in_state(symbol)
            except Exception:
                # Fallback: try state_manager directly if available
                try:
                    if state_manager:
                        await state_manager.close_trade(symbol)
                except Exception as e:
                    logger.error(f"Failed to mark {symbol} closed in state: {e}")
            
            ghost_count += 1
            await asyncio.sleep(0.1)
    
    if ghost_count > 0:
        logger.warning(f"🧹 Cleaned {ghost_count} ghost trades from DB")
    else:
        logger.info("✓ All DB trades match exchange positions")
    
    # Re-check after cleanup
    open_trades_after = await get_open_trades()
    logger.info(f"📊 Final state: {len(open_trades_after)} open trades")


async def logic_loop(lighter, x10, price_event, parallel_exec):
    """Main trading loop with opportunity detection and execution"""
    global LAST_DATA_UPDATE, LAST_ARBITRAGE_LAUNCH
    REFRESH_DELAY = getattr(config, 'REFRESH_DELAY_SECONDS', 5)
    logger.info(f"Logic Loop gestartet – REFRESH alle {REFRESH_DELAY}s")

    while not SHUTDOWN_FLAG:
        try:
            # Update watchdog heartbeat
            LAST_DATA_UPDATE = time.time()
            
            open_trades = await get_open_trades()
            open_syms = {t['symbol'] for t in open_trades}
            
            max_trades = getattr(config, 'MAX_OPEN_TRADES', 40)
            if len(open_trades) >= max_trades:
                logger.debug(f"MAX_OPEN_TRADES ({max_trades}) reached, waiting...")
                await asyncio.sleep(REFRESH_DELAY)
                continue

            opportunities = await find_opportunities(lighter, x10, open_syms, is_farm_mode=None)
            
            if opportunities:
                logger.info(f"🎯 Found {len(opportunities)} opportunities, executing best...")
                
                # ═══════════════════════════════════════════════════════════════
                # Balance Check mit robuster Exception Handling
                # ═══════════════════════════════════════════════════════════════
                try:
                    bal_x10 = await x10.get_real_available_balance()
                except Exception as e:
                    logger.error(f"❌ X10 Balance Check FAILED: {e}")
                    bal_x10 = 0.0
                
                try:
                    bal_lit = await lighter.get_real_available_balance()
                except Exception as e:
                    logger.error(f"❌ Lighter Balance Check FAILED: {e}")
                    bal_lit = 0.0

                logger.info(f"X10: Detected balance = ${bal_x10:.2f}")

                # SAFETY FIX: Removed '$50 Hack'. If balance is 0/Low, we STOP trading.
                if bal_x10 < 5.0:
                    logger.warning(
                        f"⚠️ X10 Balance too low or unreadable (${bal_x10:.2f}). "
                        "Pausing new trades until balance is restored."
                    )
                    await asyncio.sleep(REFRESH_DELAY * 2)
                    continue

                # ═══════════════════════════════════════════════════════════════
                # EXECUTION LOGIC (Race Condition Fix)
                # ═══════════════════════════════════════════════════════════════
                
                # 1. Aktuellen Status prüfen
                open_trades_refreshed = await get_open_trades()
                max_trades = getattr(config, 'MAX_OPEN_TRADES', 40)
                
                # FIX: Deduplizierung auch hier!
                open_symbols_db = {t.get('symbol') if isinstance(t, dict) else t for t in open_trades_refreshed}
                
                # WICHTIG: Variable wiederherstellen für das Logging weiter unten!
                current_open_count = len(open_symbols_db)
                
                async with TASKS_LOCK:
                    executing_symbols = set(ACTIVE_TASKS.keys())
                
                # Union bilden
                all_active_symbols = open_symbols_db | executing_symbols
                current_total_count = len(all_active_symbols)
                
                # Berechne exakt freie Slots
                slots_available = max_trades - current_total_count

                if slots_available <= 0:
                    logger.debug(f"⛔ Max trades reached ({current_total_count}/{max_trades}). Waiting...")
                    await asyncio.sleep(REFRESH_DELAY)
                    continue

                # 2. Sortieren: Die besten Opportunities zuerst (höchste APY)
                opportunities.sort(key=lambda x: x.get('apy', 0), reverse=True)

                # 3. Kandidaten auswählen (Limitiert auf slots_available)
                trades_to_launch = []
                existing_symbols = {t.get('symbol') if isinstance(t, dict) else t for t in open_trades_refreshed}

                for opp in opportunities:
                    # Wenn wir voll sind, brechen wir die Schleife sofort ab
                    if len(trades_to_launch) >= slots_available:
                        break
                    
                    symbol = opp.get('symbol') if isinstance(opp, dict) else getattr(opp, 'symbol', None)
                    if not symbol:
                        continue
                    
                    # Sicherheitscheck: Symbol schon offen?
                    if symbol in existing_symbols:
                        continue
                    
                    # Skip if already active task
                    if symbol in ACTIVE_TASKS:
                        continue
                    
                    # Skip if recently failed
                    if symbol in FAILED_COINS and (time.time() - FAILED_COINS[symbol] < 180):
                        continue
                    
                    # Opportunity zur Liste hinzufügen
                    trades_to_launch.append(opp)

                # 4. Kontrollierter Start der Trades
                if trades_to_launch:
                    global LAST_ARBITRAGE_LAUNCH
                    LAST_ARBITRAGE_LAUNCH = time.time()  # <--- NEU: Setze Zeitstempel
                    logger.info(f"🚀 Launching {len(trades_to_launch)} trades (Slots available: {slots_available})")
                    
                    # Balance check for batch
                    avg_trade_size = getattr(config, 'DESIRED_NOTIONAL_USD', 12)
                    
                    # Verify trades actually exist on exchanges
                    try:
                        x10_positions = await x10.fetch_open_positions()
                        lighter_positions = await lighter.fetch_open_positions()
                        actual_open_count = len(set(
                            [p.get('symbol') for p in (x10_positions or [])] +
                            [p.get('symbol') for p in (lighter_positions or [])]
                        ))
                    except:
                        actual_open_count = current_open_count  # Fallback
                    
                    # Use ACTUAL open count, not DB count
                    locked_per_exchange = actual_open_count * avg_trade_size * 0.6
                    
                    async with IN_FLIGHT_LOCK:
                        available_x10 = bal_x10 - IN_FLIGHT_MARGIN.get('X10', 0) - locked_per_exchange
                        available_lit = bal_lit - IN_FLIGHT_MARGIN.get('Lighter', 0) - locked_per_exchange
                    
                    # Safety buffer
                    available_x10 = max(0, available_x10 - 3.0)
                    available_lit = max(0, available_lit - 3.0)
                    
                    logger.info(
                        f"💰 Balance: X10=${bal_x10:.1f} (free=${available_x10:.1f}), "
                        f"Lit=${bal_lit:.1f} (free=${available_lit:.1f}) | "
                        f"DB_Trades={current_open_count}, Real_Positions={actual_open_count}, "
                        f"Locked=${locked_per_exchange:.0f}/ex"
                    )
                    
                    # Launch trades with proper locking
                    launched_count = 0
                    for opp in trades_to_launch:
                        symbol = opp.get('symbol') if isinstance(opp, dict) else getattr(opp, 'symbol', None)
                        if not symbol:
                            continue
                        
                        # Check balance for this trade
                        is_farm = opp.get('is_farm_trade', False)
                        base_default = getattr(config, 'FARM_NOTIONAL_USD', 12) if is_farm else getattr(config, 'DESIRED_NOTIONAL_USD', 16)
                        
                        trade_size = None
                        for key in ('trade_size', 'notional_usd', 'trade_notional_usd', 'desired_notional_usd'):
                            if key in opp and isinstance(opp.get(key), (int, float)):
                                trade_size = float(opp[key])
                                break
                        
                        if trade_size is None:
                            trade_size = float(base_default)
                        
                        required_margin = trade_size * 1.2
                        
                        if available_x10 < required_margin or available_lit < required_margin:
                            logger.debug(f"Skip {symbol}: Insufficient balance (X10=${available_x10:.1f}, Lit=${available_lit:.1f}, Need=${required_margin:.1f})")
                            continue
                        
                        # Deduct from available balance
                        available_x10 -= required_margin
                        available_lit -= required_margin
                        
                        logger.info(f"🚀 Launching trade for {symbol} (APY={opp.get('apy', 0):.1f}%)")
                        
                        # Symbol-Level Lock
                        async with TASKS_LOCK:
                            if symbol in ACTIVE_TASKS:
                                logger.debug(f"⏸️  {symbol} already has active task, skipping")
                                continue
                        
                        lock = get_symbol_lock(symbol)
                        
                        async def _handle_with_lock():
                            async with lock:
                                try:
                                    await execute_trade_parallel(opp, lighter, x10, parallel_exec)
                                finally:
                                    async with TASKS_LOCK:
                                        ACTIVE_TASKS.pop(symbol, None)
                        
                        # CRITICAL: Don't start new tasks during shutdown
                        if SHUTDOWN_FLAG:
                            logger.debug(f"Shutdown active, skipping task for {symbol}")
                            break
                        
                        task = asyncio.create_task(_handle_with_lock())
                        async with TASKS_LOCK:
                            if SHUTDOWN_FLAG:
                                task.cancel()
                                break
                            ACTIVE_TASKS[symbol] = task
                        
                        LAST_ARBITRAGE_LAUNCH = time.time()
                        launched_count += 1
                        await asyncio.sleep(0.5)
                    
                    if launched_count > 0:
                        logger.info(f"✅ Launched {launched_count} trade tasks this cycle")
            
            
            # CRITICAL: Check SHUTDOWN_FLAG before sleeping
            if SHUTDOWN_FLAG:
                logger.info("Logic loop: SHUTDOWN_FLAG detected, exiting")
                break
            await asyncio.sleep(REFRESH_DELAY)
            
        except asyncio.CancelledError:
            logger.info("Logic loop cancelled")
            break
        except Exception as e:
            logger.error(f"Logic loop error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)

async def trade_management_loop(lighter, x10):
    """Überwacht offene Trades und schließt sie nach Kriterien (Farm-Timer, TP/SL, etc.)"""
    logger.info("🛡️ Trade Management Loop gestartet")
    while not SHUTDOWN_FLAG:
        try:
            # Ruft die Logik auf, die du bereits hast, aber die nie lief
            await manage_open_trades(lighter, x10)
            await asyncio.sleep(1)  # Jede Sekunde prüfen
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Trade Management Error: {e}")
            await asyncio.sleep(5)

async def maintenance_loop(lighter, x10):
    """Background tasks: Funding rates refresh + REST Fallback für BEIDE Exchanges + Sync Check"""
    global LAST_DATA_UPDATE
    funding_refresh_interval = 30  # Alle 30s Funding Rates refreshen
    sync_check_interval = 300  # Alle 5 Minuten Sync Check
    last_x10_refresh = 0
    last_lighter_refresh = 0
    last_sync_check = 0
    
    while True:
        try:
            now = time.time()
            
            # Update watchdog heartbeat
            LAST_DATA_UPDATE = now
            
            # ============================================================
            # X10 Funding Refresh (market_info neu laden enthält Funding)
            # ============================================================
            x10_funding_count = len(getattr(x10, 'funding_cache', {}))
            
            if x10_funding_count < 20 or (now - last_x10_refresh > 60):
                logger.debug(f"📊 X10 Funding Cache refresh (current={x10_funding_count})")
                try:
                    # X10 market_info enthält funding_rate
                    await x10.load_market_cache(force=True)
                    last_x10_refresh = now
                    logger.debug(f"X10: Funding Refresh OK, cache={len(x10.funding_cache)}")
                except Exception as e:
                    logger.warning(f"X10 Funding Refresh failed: {e}")

            # ============================================================
            # Lighter Funding Refresh
            # ============================================================
            lighter_funding_count = len(getattr(lighter, 'funding_cache', {}))
            
            if lighter_funding_count < 50 or (now - last_lighter_refresh > 60):
                logger.debug(f"📊 Lighter Funding Cache refresh (current={lighter_funding_count})")
                try:
                    await lighter.load_funding_rates_and_prices()
                    last_lighter_refresh = now
                    logger.debug(f"Lighter: Funding Refresh OK, cache={len(lighter.funding_cache)}")
                except Exception as e:
                    logger.warning(f"Lighter Funding Refresh failed: {e}")
            
            # --- Update predictor with fresh funding/OB/OI data ---
            try:
                predictor = get_predictor()
                common = set(getattr(lighter, 'market_info', {}).keys()) & set(getattr(x10, 'market_info', {}).keys())
                
                for symbol in list(common)[:50]:  # Limit to 50 symbols per cycle
                    try:
                        r_l = lighter.fetch_funding_rate(symbol) or 0.0
                        r_x = x10.fetch_funding_rate(symbol) or 0.0
                        current_rate = (float(r_l) + float(r_x)) / 2.0

                        # OI
                        oi = 0.0
                        try:
                            oi = await x10.fetch_open_interest(symbol)
                        except Exception:
                            pass
                        if oi == 0.0:
                            try:
                                oi = await lighter.fetch_open_interest(symbol)
                            except Exception:
                                pass

                        predictor._update_history(predictor.rate_history, symbol, float(current_rate), now)
                        predictor.update_oi_velocity(symbol, float(oi))
                        
                    except Exception:
                        continue
            except Exception:
                pass
            
            # ============================================================
            # Exchange Sync Check (every 5 minutes)
            # ============================================================
            if now - last_sync_check > sync_check_interval:
                logger.info("🔍 Running periodic exchange sync check...")
                try:
                    await sync_check_and_fix(lighter, x10)
                    last_sync_check = now
                except Exception as e:
                    logger.error(f"Sync check failed: {e}")
            
            await asyncio.sleep(30)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Maintenance loop error: {e}")
            await asyncio.sleep(30)

async def setup_database():
    """Initialize async database"""
    db = await get_database()
    logger.info("✅ Async database initialized")
#
async def migrate_database():
    """Migrate DB schema for new columns"""
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            # FIRST: Create trade_history table if it doesn't exist
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
            
            # Check if migration needed for existing table
            cursor = await conn.execute("PRAGMA table_info(trade_history)")
            columns = await cursor.fetchall()
            has_account_label = any(col[1] == 'account_label' for col in columns)
            
            if not has_account_label:
                logger.info("🔄 Migrating database schema...")
                try:
                    await conn.execute("ALTER TABLE trade_history ADD COLUMN account_label TEXT DEFAULT 'Main'")
                except Exception:
                    pass  # Column might already exist
                try:
                    await conn.execute("ALTER TABLE trades ADD COLUMN account_label TEXT DEFAULT 'Main'")
                except Exception:
                    pass  # Column might already exist
                await conn.commit()
                logger.info("✅ Database migration complete")
            else:
                logger.debug("✅ Database schema up to date")
                
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        # DON'T delete the database - just continue
        logger.warning("⚠️ Continuing without migration...")

# ═══════════════════════════════════════════════════════════════════════════════
# PUNKT 2: NON-BLOCKING MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════
# Umbau auf asyncio Task-Management mit:
# - Structured Concurrency (TaskGroup-ähnlich)
# - Graceful Shutdown mit Signal Handlers
# - Task Supervision & Auto-Restart
# - Health Monitoring
# ═══════════════════════════════════════════════════════════════════════════════

class TaskState(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RESTARTING = "RESTARTING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class ManagedTask:
    """Tracks state of a managed background task"""
    name: str
    coro_factory: Callable[[], Awaitable[Any]]
    task: Optional[asyncio.Task] = None
    state: TaskState = TaskState.PENDING
    restart_count: int = 0
    max_restarts: int = 5
    restart_delay: float = 5.0
    last_error: Optional[str] = None
    started_at: Optional[float] = None
    critical: bool = True  # If True, shutdown bot on permanent failure


class TaskSupervisor:
    """
    Manages async tasks with supervision, restart, and graceful shutdown. 
    
    Features:
    - Auto-restart failed tasks with exponential backoff
    - Graceful shutdown on SIGINT/SIGTERM
    - Health monitoring
    - Task isolation (one crash doesn't kill others)
    """
    
    def __init__(self):
        self.tasks: Dict[str, ManagedTask] = {}
        self.shutdown_event = asyncio.Event()
        self._shutdown_timeout = 30.0
        self._health_check_interval = 10.0
        self._signal_handlers_installed = False
        self._supervisor_task: Optional[asyncio.Task] = None
        
    def register(
        self,
        name: str,
        coro_factory: Callable[[], Awaitable[Any]],
        critical: bool = True,
        max_restarts: int = 5,
        restart_delay: float = 5.0
    ):
        """Register a task to be supervised"""
        self.tasks[name] = ManagedTask(
            name=name,
            coro_factory=coro_factory,
            critical=critical,
            max_restarts=max_restarts,
            restart_delay=restart_delay
        )
        logger.info(f"📝 Registered task: {name} (critical={critical})")
        
    def _install_signal_handlers(self):
        """Install signal handlers for graceful shutdown"""
        if self._signal_handlers_installed:
            return
            
        loop = asyncio.get_running_loop()
        
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self._signal_handler(s))
                )
                logger.debug(f"Installed handler for {sig.name}")
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, lambda s, f: asyncio.create_task(self._signal_handler(s)))
                
        self._signal_handlers_installed = True
        logger.info("✅ Signal handlers installed (SIGINT, SIGTERM)")
        
    async def _signal_handler(self, sig):
        """Handle shutdown signal"""
        sig_name = signal.Signals(sig).name if isinstance(sig, int) else str(sig)
        logger.warning(f"🛑 Received {sig_name}, initiating graceful shutdown...")
        self.shutdown_event.set()
        
    async def start_all(self):
        """Start all registered tasks"""
        self._install_signal_handlers()
        
        for name, managed in self.tasks.items():
            await self._start_task(managed)
            
        # Start supervisor loop
        self._supervisor_task = asyncio.create_task(
            self._supervisor_loop(),
            name="supervisor"
        )
        
        logger.info(f"🚀 Started {len(self.tasks)} tasks")
        
    async def _start_task(self, managed: ManagedTask):
        """Start a single managed task"""
        try:
            managed.task = asyncio.create_task(
                self._task_wrapper(managed),
                name=managed.name
            )
            managed.state = TaskState.RUNNING
            managed.started_at = time.monotonic()
            logger.info(f"▶️  Started task: {managed.name}")
        except Exception as e:
            managed.state = TaskState.FAILED
            managed.last_error = str(e)
            logger.error(f"❌ Failed to start task {managed.name}: {e}")
            
    async def _task_wrapper(self, managed: ManagedTask):
        """Wrapper that catches exceptions and enables restart"""
        try:
            await managed.coro_factory()
        except asyncio.CancelledError:
            managed.state = TaskState.CANCELLED
            logger.info(f"🛑 Task {managed.name} cancelled")
            raise
        except Exception as e:
            managed.state = TaskState.FAILED
            managed.last_error = str(e)
            logger.error(f"❌ Task {managed.name} crashed: {e}", exc_info=True)
            raise
            
    async def _supervisor_loop(self):
        """Monitor tasks and restart failed ones"""
        logger.info("🔍 Supervisor loop started")
        
        while not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(self._health_check_interval)
                
                for name, managed in self.tasks.items():
                    if managed.task is None:
                        continue
                        
                    if managed.task.done():
                        # Task finished or crashed
                        if managed.state == TaskState.CANCELLED:
                            continue
                            
                        # Check for exception
                        try:
                            exc = managed.task.exception()
                            if exc:
                                managed.last_error = str(exc)
                        except asyncio.CancelledError:
                            continue
                            
                        # Attempt restart
                        if managed.restart_count < managed.max_restarts:
                            managed.restart_count += 1
                            managed.state = TaskState.RESTARTING
                            
                            delay = managed.restart_delay * (2 ** (managed.restart_count - 1))
                            delay = min(delay, 60.0)  # Cap at 60s
                            
                            logger.warning(
                                f"🔄 Restarting {name} in {delay:.1f}s "
                                f"(attempt {managed.restart_count}/{managed.max_restarts})"
                            )
                            
                            await asyncio.sleep(delay)
                            
                            if not self.shutdown_event.is_set():
                                await self._start_task(managed)
                        else:
                            managed.state = TaskState.FAILED
                            logger.error(
                                f"❌ Task {name} exceeded max restarts ({managed.max_restarts})"
                            )
                            
                            if managed.critical:
                                logger.critical(f"💀 Critical task {name} failed, initiating shutdown")
                                self.shutdown_event.set()
                                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Supervisor error: {e}")
                
        logger.info("🔍 Supervisor loop stopped")
        
    async def wait_for_shutdown(self):
        """Wait for shutdown signal"""
        await self.shutdown_event.wait()
        
    async def shutdown(self):
        """Gracefully shutdown all tasks"""
        logger.info("🛑 Initiating graceful shutdown...")
        self.shutdown_event.set()
        
        # Cancel supervisor first
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await asyncio.wait_for(self._supervisor_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
                
        # Cancel all managed tasks
        tasks_to_cancel = []
        for name, managed in self.tasks.items():
            if managed.task and not managed.task.done():
                managed.task.cancel()
                tasks_to_cancel.append(managed.task)
                logger.info(f"🛑 Cancelling {name}...")
                
        if tasks_to_cancel:
            # Wait for all tasks with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=self._shutdown_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ Shutdown timeout ({self._shutdown_timeout}s) exceeded")
                
        # Update states
        for managed in self.tasks.values():
            if managed.state == TaskState.RUNNING:
                managed.state = TaskState.STOPPED
                
        logger.info("✅ All tasks stopped")
        
    def get_health_report(self) -> Dict[str, Any]:
        """Get health status of all tasks"""
        report = {
            "shutdown_requested": self.shutdown_event.is_set(),
            "tasks": {}
        }
        
        for name, managed in self.tasks.items():
            task_info = {
                "state": managed.state.value,
                "restart_count": managed.restart_count,
                "critical": managed.critical,
            }
            
            if managed.started_at:
                task_info["uptime_seconds"] = time.monotonic() - managed.started_at
                
            if managed.last_error:
                task_info["last_error"] = managed.last_error
                
            if managed.task:
                task_info["done"] = managed.task.done()
                
            report["tasks"][name] = task_info
            
        return report


# ============================================================
# IMPORTS FÜR V5 COMPONENT WIRING
# ============================================================
from src.open_interest_tracker import init_oi_tracker, get_oi_tracker
from src.websocket_manager import init_websocket_manager, get_websocket_manager
from src.prediction_v2 import get_predictor

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT RUNNER V5 (Task Supervised & Component Wired)
# ═══════════════════════════════════════════════════════════════════════════════

async def run_bot_v5():
    """
    Main bot entry point with full task supervision and component wiring.
    
    Activates:
    - Task Supervisor (Auto-Restart)
    - Open Interest Tracking
    - WebSocket Streaming
    - Prediction V2
    """
    global SHUTDOWN_FLAG, state_manager, telegram_bot
    
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
    
    # 2. INIT ADAPTERS
    x10 = X10Adapter()
    lighter = LighterAdapter()
    
    price_event = asyncio.Event()
    x10.price_update_event = price_event
    lighter.price_update_event = price_event
    
    # 3. LOAD MARKET DATA (CRITICAL: Before WS/OI)
    logger.info("📊 Loading Market Data via REST...")
    try:
        # Parallel load with timeout
        await asyncio.wait_for(
            asyncio.gather(
                x10.load_market_cache(force=True),
                lighter.load_market_cache(force=True),
                return_exceptions=True
            ),
            timeout=60.0
        )
        logger.info(f"✅ Markets loaded: X10={len(x10.market_info)}, Lighter={len(lighter.market_info)}")
        
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: Load Lighter prices BEFORE WebSocket starts
        # ═══════════════════════════════════════════════════════════════
        logger.info("📈 Pre-loading Lighter prices...")
        try:
            await lighter.load_funding_rates_and_prices()
            logger.info(f"✅ Lighter prices loaded: {len(lighter.price_cache)} symbols")
        except Exception as e:
            logger.warning(f"⚠️ Lighter price preload warning: {e}")
        
        # REMOVED: 404 causing initial fetch
        # Pre-fetch funding rates logic moved to WS warmup in find_opportunities
        
    except Exception as e:
        logger.error(f"⚠️ Market load warning (continuing): {e}")

    # 4. PRE-WARMUP EXECUTION CLIENTS (CRITICAL FOR SPEED)
    logger.info("🔥 Warming up execution clients (Network Handshake)...")
    try:
        # 1. Lighter Signer Warmup (lädt WASM)
        await lighter._get_signer()
        
        # 2. X10 Network Warmup (Echter HTTP Request!)
        # Dieser Call zwingt aiohttp, die SSL-Verbindung jetzt schon aufzubauen
        x10_client = await x10._get_trading_client()
        await x10_client.markets_info.get_markets()
        
        logger.info("✅ Execution clients warmed up & NETWORK READY")
    except Exception as e:
        logger.warning(f"⚠️ Warmup warning: {e}")

    # 5. INIT COMPONENTS & WIRING (Das fehlte vorher!)
    # ---------------------------------------------------
    
    # A) Prediction Engine holen
    predictor = get_predictor()
    
    # B) Open Interest Tracker starten
    # Sammelt OI Daten via REST und bereitet WS Updates vor
    common_symbols = list(set(x10.market_info.keys()) & set(lighter.market_info.keys()))
    logger.info(f"启动 OI Tracker für {len(common_symbols)} Symbole...")
    oi_tracker = await init_oi_tracker(x10, lighter, symbols=common_symbols)
    
    # C) WebSocket Manager starten & verknüpfen
    logger.info("🌐 Starting WebSocket Manager...")
    ws_manager = await init_websocket_manager(
        x10, 
        lighter, 
        symbols=common_symbols,
        # X10 Heartbeat-Strategie: JSON-Level Heartbeats!
        # ping_interval=None aktiviert den JSON-Heartbeat-Modus:
        # - Sendet {"type": "PING", "timestamp": ...} alle 15 Sekunden
        # - Dies ist was das offizielle X10 TypeScript SDK tut
        # - Löst das "1011 Ping timeout" Problem
        ping_interval=None, 
        ping_timeout=None
    )
    
    # D) WIRING: WebSocket -> OI Tracker & Predictor
    # Damit fließen Echtzeit-Daten in die Prediction Logik
    ws_manager.set_oi_tracker(oi_tracker)
    ws_manager.set_predictor(predictor)
    
    logger.info("🔗 Components Wired: WS -> OI Tracker -> Prediction")

    # --- ZOMBIE KILLER: DB mit Realität abgleichen ---
    # Führe direkt nach Adapter- und WebSocket-Init einen Abgleich durch,
    # bevor der Supervisor startet, um Geistereinträge zu bereinigen.
    logger.info("🧟 Starte Zombie-Check: Vergleiche DB mit echten Positionen...")

    # 1. Hole die "vermeintlich" offenen Trades aus der DB
    try:
        db_trades = await get_open_trades()
    except Exception as e:
        logger.warning(f"Zombie-Check: Konnte DB-Trades nicht laden: {e}")
        db_trades = []

    if db_trades:
        logger.info(f"🔍 DB meldet {len(db_trades)} offene Trades. Prüfe Exchange...")

        # 2. Hole die ECHTEN Positionen von den Börsen
        try:
            real_x10_pos = await x10.fetch_open_positions()
        except Exception as e:
            logger.warning(f"Zombie-Check: X10 Positions-Load fehlgeschlagen: {e}")
            real_x10_pos = []
        try:
            real_lighter_pos = await lighter.fetch_open_positions()
        except Exception as e:
            logger.warning(f"Zombie-Check: Lighter Positions-Load fehlgeschlagen: {e}")
            real_lighter_pos = []

        # Erstelle eine Liste aller Symbole, die wirklich offen sind
        def _get_sym(p):
            if isinstance(p, dict):
                return p.get('symbol')
            return getattr(p, 'symbol', None)

        real_symbols: List[str] = []
        if real_x10_pos:
            real_symbols.extend([_get_sym(p) for p in real_x10_pos if _get_sym(p)])
        if real_lighter_pos:
            real_symbols.extend([_get_sym(p) for p in real_lighter_pos if _get_sym(p)])

        logger.info(f"🌍 Echte Positionen auf Exchanges: {real_symbols}")

        # 3. Vergleiche und bereinige
        for trade in db_trades:
            tsym = trade.get('symbol') if isinstance(trade, dict) else getattr(trade, 'symbol', None)
            if not tsym:
                continue
            if tsym not in real_symbols:
                logger.warning(
                    f"⚠️  ZOMBIE GEFUNDEN: {tsym} ist in DB offen, aber nicht auf Exchange. Schließe ihn..."
                )
                # Markiere im State als geschlossen; stille Bereinigung ohne Exchange-Action
                try:
                    await close_trade_in_state(tsym)
                except Exception:
                    try:
                        if state_manager:
                            await state_manager.close_trade(tsym)
                    except Exception as e:
                        logger.error(f"Zombie-Check: DB-Close für {tsym} fehlgeschlagen: {e}")
    else:
        logger.info("✅ DB ist sauber (keine offenen Trades).")
    # --- ENDE ZOMBIE KILLER ---

    # 6. INIT PARALLEL EXECUTION MANAGER
    parallel_exec = ParallelExecutionManager(x10, lighter)
    await parallel_exec.start()
    logger.info("✅ ParallelExecutionManager started")
    
    # 7. CREATE TASK SUPERVISOR
    supervisor = TaskSupervisor()
    
    # Register core tasks
    supervisor.register(
        "logic_loop",
        lambda: logic_loop(lighter, x10, price_event, parallel_exec),
        critical=True,
        max_restarts=999 # Infinite restarts for main logic
    )
    
    supervisor.register(
        "trade_management_loop",
        lambda: trade_management_loop(lighter, x10),
        critical=True,
        max_restarts=999
    )
    
    supervisor.register(
        "farm_loop",
        lambda: farm_loop(lighter, x10, parallel_exec),
        critical=False,
        max_restarts=10
    )
    
    # Maintenance Loop (Funding Rates Refresh)
    supervisor.register(
        "maintenance_loop",
        lambda: maintenance_loop(lighter, x10),
        critical=False,
        max_restarts=10
    )
    
    # Cleanup Tasks
    supervisor.register(
        "cleanup_finished_tasks",
        lambda: cleanup_finished_tasks(),
        critical=False,
        max_restarts=5
    )
    
    # Health Reporter
    supervisor.register(
        "health_reporter",
        lambda: health_reporter(supervisor, parallel_exec),
        critical=False,
        max_restarts=5
    )
    
    # Connection Watchdog (gegen Ping Timeout)
    supervisor.register(
        "connection_watchdog",
        lambda: connection_watchdog(ws_manager, x10, lighter),
        critical=True,  # Critical, da ohne Daten kein Trading möglich
        max_restarts=999
    )
    
    # 8. START ALL TASKS
    await supervisor.start_all()
    
    if telegram_bot and telegram_bot.enabled:
        await telegram_bot.send_message(
            "🚀 **Funding Bot V5 Started**\n"
            f"OI Tracker: Active ({len(common_symbols)} syms)\n"
            "Mode: Supervised"
        )
    
    logger.info("═══════════════════════════════════════════════════════════")
    logger.info("   BOT V5 RUNNING 24/7 - SUPERVISED | Ctrl+C = Stop   ")
    logger.info("═══════════════════════════════════════════════════════════")
    
    # 9. WAIT FOR SHUTDOWN
    try:
        await supervisor.wait_for_shutdown()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("🛑 Shutdown requested via keyboard")
        
    # 10. GRACEFUL SHUTDOWN SEQUENCE
    SHUTDOWN_FLAG = True
    logger.info("🛑 Shutting down...")
    
    if telegram_bot and telegram_bot.enabled:
        await telegram_bot.send_message("🛑 **Bot shutting down...**")
        
    # Stop components in reverse order
    await supervisor.shutdown()
    await parallel_exec.stop()
    
    if ws_manager:
        await ws_manager.stop()
        
    if oi_tracker:
        await oi_tracker.stop()
    
    await close_state_manager()
    
    if telegram_bot and telegram_bot.enabled:
        await telegram_bot.stop()
    
    await close_database()
    
    # Close adapters
    await x10.aclose()
    await lighter.aclose()
        
    logger.info("✅ Bot V5 shutdown complete")


async def health_reporter(supervisor: TaskSupervisor, parallel_exec):
    """Periodic health status reporter"""
    interval = 3600  # 1 hour
    telegram = get_telegram_bot()
    
    while not supervisor.shutdown_event.is_set():
        try:
            await asyncio.sleep(interval)
            
            health = supervisor.get_health_report()
            exec_stats = parallel_exec.get_execution_stats()
            
            # Get prediction stats
            predictor = get_predictor()
            pred_stats = predictor.get_stats()
            
            # Log health
            running = sum(1 for t in health["tasks"].values() if t["state"] == "RUNNING")
            total = len(health["tasks"])
            
            logger.info(
                f"📊 Health: {running}/{total} tasks running, "
                f"{exec_stats.get('total_executions', 0)} trades, "
                f"{exec_stats.get('pending_rollbacks', 0)} pending rollbacks"
            )
            
            logger.info(
                f"📊 Prediction: {pred_stats['predictions_made']} predictions, "
                f"avg confidence: {pred_stats['avg_confidence']:.2f}"
            )
            
            # Send to Telegram every 6 hours
            if interval >= 3600 and telegram.enabled:
                msg = (
                    f"📊 **Health Report**\n"
                    f"Tasks: {running}/{total} running\n"
                    f"Trades: {exec_stats.get('successful', 0)} ✅ / {exec_stats.get('failed', 0)} ❌\n"
                    f"Rollbacks: {exec_stats.get('rollbacks_successful', 0)} ✅ / {exec_stats.get('rollbacks_failed', 0)} ❌"
                )
                await telegram.send_message(msg)
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Health reporter error: {e}")
            await asyncio.sleep(60)


# ============================================================
# MAIN (Original V4 - kept for compatibility)
# ============================================================
async def main():
    global SHUTDOWN_FLAG, state_manager
    logger.info(" 🔥 BOT V4 (Full Architected) STARTING...")

    # 1. Init Infrastructure
    from src.state_manager import get_state_manager as get_sm_async
    state_manager = await get_sm_async()
    logger.info("✅ State Manager started")
    global telegram_bot
    telegram_bot = get_telegram_bot()
    if telegram_bot.enabled:
        await telegram_bot.start()
        logger.info("📱 Telegram Bot connected")
    await setup_database()
    await migrate_database()
    
    # 2. Init Adapters
    x10 = X10Adapter()
    lighter = LighterAdapter()
    price_event = asyncio.Event()
    x10.price_update_event = price_event
    lighter.price_update_event = price_event
    
    # 3. Load Market Data FIRST (CRITICAL - Required for WebSocket subscriptions)
    logger.info("📊 Loading Market Data...")
    try:
        # PARALLEL mit TIMEOUT statt sequential
        load_results = await asyncio.wait_for(
            asyncio.gather(
                x10.load_market_cache(force=True),
                lighter.load_market_cache(force=True),
                return_exceptions=True
            ),
            timeout=60.0  # 60 Sekunden max für beide
        )

        # Check for errors from the parallel load
        for i, result in enumerate(load_results):
            if isinstance(result, Exception):
                exchange = "X10" if i == 0 else "Lighter"
                logger.error(f"❌ {exchange} market load failed: {result}")

        x10_count = len(x10.market_info)
        lit_count = len(lighter.market_info)

        logger.info(f"✅ Markets loaded: X10={x10_count}, Lighter={lit_count}")

        # Nach "✅ Markets loaded" und VOR WebSocket-Start: Funding Rates vorladen
        logger.info("📈 Pre-loading funding rates...")

        # Lighter funding rates laden
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://mainnet.zklighter.elliot.ai/api/v1/funding-rates") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for fr in data.get("funding_rates", []):
                            if fr.get("exchange") == "lighter":
                                symbol = fr.get("symbol")
                                rate = fr.get("rate")
                                if symbol and rate is not None:
                                    # Normalize symbol to include -USD
                                    if not symbol.endswith("-USD"):
                                        symbol = f"{symbol}-USD"
                                    lighter.funding_cache[symbol] = float(rate)
                        logger.info(f"✅ Lighter funding rates pre-loaded: {len(lighter.funding_cache)}")
        except Exception as e:
            logger.warning(f"⚠️ Lighter funding preload: {e}")

        # X10 funding rates (bereits in market_cache enthalten)
        await x10.load_market_cache(force=True)
        logger.info(f"✅ X10 funding rates: {len(x10.funding_cache)}")

    except asyncio.TimeoutError:
        logger.error("❌ Market loading TIMEOUT (60s) - continuing with partial data")
        x10_count = len(x10.market_info)
        lit_count = len(lighter.market_info)
        logger.info(f"⚠️ Partial markets: X10={x10_count}, Lighter={lit_count}")
        
        if x10_count == 0 and lit_count == 0:
            raise ValueError("No markets loaded from any exchange after timeout")

        if x10_count == 0 and lit_count == 0:
            raise ValueError("No markets loaded from any exchange")
    
        if lit_count == 0:
            logger.warning("⚠️ Lighter markets not loaded - bot will use X10 only")
    
        if x10_count == 0:
            logger.warning("⚠️ X10 markets not loaded - bot will use Lighter only")
            # Load initial funding rates
            logger.info("📈 Loading initial funding rates...")
            await lighter.load_funding_rates_and_prices()
            # Verify data loaded
            test_syms = ["BTC-USD", "ETH-USD", "SOL-USD"]
            loaded_ok = False
            for sym in test_syms:
                if sym in x10.market_info and sym in lighter.market_info:
                    px = safe_float(x10.fetch_mark_price(sym))
                    pl = safe_float(lighter.fetch_mark_price(sym))
                    if px > 0 and pl > 0:
                        logger.info(f"✅ Data OK: {sym} X10=${px:.2f}, Lighter=${pl:.2f}")
                        loaded_ok = True
                        break
            if not loaded_ok:
                raise ValueError("No valid market data after load")
    
    except Exception as e:
        logger.critical(f"❌ FATAL: Market data load failed: {e}")
        return
    
    # 4. NOW Start WebSocket Streams (AFTER markets loaded)
    logger.info("🌐 Starting WebSocket streams...")
    ws_manager = WebSocketManager()
    ws_manager.set_adapters(x10, lighter)
    cleanup_task = None

    # DEBUG: Log before start
    logger.info("DEBUG: About to call ws_manager.start()...")

    try:
        await asyncio.wait_for(ws_manager.start(), timeout=30.0)
        logger.info("✅ WebSocket streams connected")
    except asyncio.TimeoutError:
        logger.warning("⚠️ WebSocket connection timeout (30s) - continuing with REST fallback")
    except Exception as e:
        logger.error(f"❌ WebSocket Manager failed to start: {e}")
        import traceback
        traceback.print_exc()

    # DEBUG: Log after start
    logger.info("DEBUG: ws_manager.start() completed or failed, continuing...")

    # ═══════════════════════════════════════════════════════════════════════════════
    # CRITICAL: Task Cleanup verhindert Memory Leaks
    # ═══════════════════════════════════════════════════════════════════════════════
    # cleanup_task wird später in `tasks` erstellt; initialisiert als None weiter oben

    # ═══════════════════════════════════════════════════════════════
    # CRITICAL: Reconcile DB state with exchange reality
    # ═══════════════════════════════════════════════════════════════
    try:
        await reconcile_db_with_exchanges(lighter, x10)
    except Exception as e:
        logger.exception(f"Reconciliation failed: {e}")
    await asyncio.sleep(2)
    
    # ═══════════════════════════════════════════════════════════════
    # CRITICAL: Initial Exchange Sync Check
    # Catches any desyncs from previous runs before starting trading
    # ═══════════════════════════════════════════════════════════════
    logger.info("🔍 Running startup exchange sync check...")
    try:
        await sync_check_and_fix(lighter, x10)
    except Exception as e:
        logger.error(f"Startup sync check failed: {e}")
    await asyncio.sleep(2)

    # ═══════════════════════════════════════════════════════════════
    # EMERGENCY: Close all positions on start if enabled
    # Set EMERGENCY_CLOSE_ON_START=True in config to activate
    # ═══════════════════════════════════════════════════════════════
    if getattr(config, 'EMERGENCY_CLOSE_ON_START', False):
        await close_all_open_positions_on_start(lighter, x10)
        await asyncio.sleep(5)

    # ═══════════════════════════════════════════════════════════════
    # CRITICAL: Emergency cleanup for stuck trades
    # ═══════════════════════════════════════════════════════════════
    emergency_task = asyncio.create_task(
        emergency_position_cleanup(lighter, x10, max_age_hours=48.0)
    )
    
    # Wait for WS connections and verify health
    logger.info("⏳ Waiting for WebSocket connections...")
    await asyncio.sleep(5)
    
    # Verify WebSocket streams are receiving data
    logger.info("🔍 Verifying WebSocket health...")
    for check in range(3):
        await asyncio.sleep(2)
        x10_cache_size = len(x10.price_cache)
        lit_cache_size = len(lighter.price_cache)
        
        logger.info(f"📊 Cache status: X10={x10_cache_size} prices, Lighter={lit_cache_size} prices")
        
        if x10_cache_size > 0 or lit_cache_size > 0:
            logger.info("✅ WebSocket streams healthy - receiving price updates")
            # Refresh missing prices for symbols without trades
            logger.info("📊 Refreshing missing prices...")
            try:
                await x10.refresh_missing_prices()
            except Exception as e:
                logger.warning(f"Price refresh warning: {e}")
            break
            
        if check == 2:
            logger.warning("⚠️ WebSocket streams not receiving data - proceeding anyway")

    # 5. Init Managers
    parallel_exec = ParallelExecutionManager(x10, lighter)
    
    # ===============================================
    # PUNKT 1 – KORREKTE ENDLESS TASK SUPERVISION
    # ===============================================
    tasks = [
        asyncio.create_task(logic_loop(lighter, x10, price_event, parallel_exec), name="logic_loop"),
        asyncio.create_task(farm_loop(lighter, x10, parallel_exec), name="farm_loop"),
        asyncio.create_task(maintenance_loop(lighter, x10), name="maintenance_loop"),
        asyncio.create_task(cleanup_finished_tasks(), name="cleanup_finished_tasks"),   # ← PUNKT 2
        asyncio.create_task(connection_watchdog(ws_manager, x10, lighter), name="connection_watchdog"),  # ← Watchdog gegen Ping Timeout

        # ➤ NEU: Dieser Task hat gefehlt! Er schließt die Trades.
        asyncio.create_task(trade_management_loop(lighter, x10), name="trade_management_loop")
    ]

    # Task-Überwachung: Eine Task crasht → Bot läuft weiter
    def task_watcher(task: asyncio.Task):
        if task.cancelled():
            logger.info(f"[SUPERVISOR] Task {task.get_name()} cancelled")
            return
        if exc := task.exception():
            logger.critical(f"[SUPERVISOR] Task {task.get_name()} crashed!", exc_info=exc)
            if telegram_bot and telegram_bot.enabled:
                asyncio.create_task(telegram_bot.send_error(f"Task Crash: {task.get_name()}\n{exc}"))

    for t in tasks:
        t.add_done_callback(task_watcher)

    logger.info("BOT LÄUFT 24/7 – Tasks überwacht | Ctrl+C = Stop")

    # WICHTIG: Kein gather mehr! Nur Endlosschleife bis Ctrl+C
    try:
        while not SHUTDOWN_FLAG:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown angefordert – beende sauber...")
    finally:
        logger.info("🛑 Initiating graceful shutdown...")
        
        # Stop tasks
        SHUTDOWN_FLAG = True
        for name, task in ACTIVE_TASKS.items():
            logger.info(f"🛑 Cancelling {name}...")
            task.cancel()
        
        # Warten bis Tasks beendet sind
        await asyncio.sleep(1.0)

        # ═══════════════════════════════════════════════════════════════
        # FIX: Close All Positions on Shutdown
        # ═══════════════════════════════════════════════════════════════
        logger.info("🚨 Closing all open positions before final shutdown...")
        try:
            # Wir holen alle offenen Trades direkt aus dem State Manager
            open_trades = await state_manager.get_active_trades()
            
            if open_trades:
                logger.info(f"🚨 Found {len(open_trades)} open trades to close...")
                close_tasks = []
                for trade in open_trades:
                    # Nutze die existierende close_trade Funktion aus diesem Script
                    logger.info(f"   -> Scheduling close for {trade['symbol']}")
                    close_tasks.append(close_trade(trade, lighter, x10))
                
                if close_tasks:
                    results = await asyncio.gather(*close_tasks, return_exceptions=True)
                    logger.info(f"   -> Shutdown Close Results: {results}")
            else:
                logger.info("✅ No open trades found in State Manager.")

            # ZUSÄTZLICHE SICHERHEIT: Exchange-Scan auf verwaiste Positionen
            logger.info("🔍 Scanning exchanges for orphaned positions...")
            try:
                lit_pos = await lighter.fetch_open_positions()
                for p in lit_pos:
                    if abs(safe_float(p.get('size', 0))) > 0:
                        logger.warning(f"⚠️ Orphaned Lighter position found: {p['symbol']}. Closing...")
                        price = safe_float(lighter.fetch_mark_price(p['symbol']))
                        size = safe_float(p.get('size', 0))
                        if price > 0:
                            try:
                                await lighter.close_live_position(p['symbol'], "BUY", abs(size) * price)
                            except TypeError as te:
                                logger.critical(f"🚨 TypeError closing Lighter {p['symbol']} on shutdown: {te}")
                
                x10_pos = await x10.fetch_open_positions()
                for p in x10_pos:
                    if abs(safe_float(p.get('size', 0))) > 0:
                        logger.warning(f"⚠️ Orphaned X10 position found: {p['symbol']}. Closing...")
                        price = safe_float(x10.fetch_mark_price(p['symbol']))
                        size = safe_float(p.get('size', 0))
                        if price > 0:
                            try:
                                await x10.close_live_position(p['symbol'], "BUY", abs(size) * price)
                            except TypeError as te:
                                logger.critical(f"🚨 TypeError closing X10 {p['symbol']} on shutdown: {te}")

            except Exception as ex_scan:
                logger.error(f"Error scanning/closing orphans: {ex_scan}")

        except Exception as e:
            logger.error(f"CRITICAL ERROR during shutdown close: {e}")
        # ═══════════════════════════════════════════════════════════════

        logger.info("🛑 Stopping InMemoryStateManager...")
        await state_manager.stop()
        
        logger.info("🔒 Closing database...")
        await close_database()
        
        # Close Adapters
        logger.info("🔌 Closing adapters...")
        if x10: await x10.aclose()
        if lighter: await lighter.aclose()
        
        logger.info("✅ Bot V5 shutdown complete")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        # SWITCHOVER TO V5 RUNNER
        asyncio.run(run_bot_v5())
    except KeyboardInterrupt:
        pass