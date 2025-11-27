# monitor_funding.py
import sys
import os
import time
import asyncio
import inspect
import aiosqlite
import logging
import random
import traceback
from datetime import datetime, timezone
from typing import Optional, List, Dict
from decimal import Decimal

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
from src.state_manager import InMemoryStateManager
from src.adaptive_threshold import get_threshold_manager
from src.volatility_monitor import get_volatility_monitor
from src.parallel_execution import ParallelExecutionManager
from src.account_manager import get_account_manager
from src.websocket_manager import WebSocketManager
from src.prediction import calculate_smart_size

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

# ============================================================
# GLOBALS (Ergänzung)
# ============================================================
# Behalte IN_FLIGHT_MARGIN von vorher!
# Define in-flight reservation globals (used when reserving trade notional)
IN_FLIGHT_MARGIN = {'X10': 0.0, 'Lighter': 0.0}
IN_FLIGHT_LOCK = asyncio.Lock()
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
                get_volatility_monitor().update_price(symbol, float(px))
            except Exception:
                pass

            net = rl - rx
            apy = abs(net) * 24 * 365  # Calculate annualized percentage yield

            # Threshold check
            threshold_manager = get_threshold_manager()
            req_apy = threshold_manager.get_threshold(symbol, is_maker=True)
            if apy < req_apy:
                logger.debug(f"{symbol}: APY {apy*100:.2f}% < required {req_apy*100:.2f}%")
                return

            # Price validity & spread
            try:
                px_f = float(px)
                pl_f = float(pl)
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
def get_state_manager():
    return state_manager

# ============================================================
# STATE & DB WRAPPERS
# ============================================================
state_manager = None

async def get_open_trades() -> List[Dict]:
    return state_manager.get_open_trades() if state_manager else []

async def add_trade_to_state(trade_data: Dict):
    if state_manager: await state_manager.add_trade(trade_data)

async def close_trade_in_state(symbol: str):
    if state_manager: await state_manager.close_trade(symbol)

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

    mode_indicator = "🚜 FARM" if is_farm_mode else "💎 ARB"
    logger.info(f"🔍 {mode_indicator} Scanning {len(common)} pairs. Open symbols to skip: {open_syms}")
    # Verify market data is loaded
    if not common:
        logger.warning("⚠️ No common markets found")
        logger.debug(f"X10 markets: {len(x10.market_info)}, Lighter: {len(lighter.market_info)}")
        return []
    
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
    for r in results:
        if isinstance(r, Exception):
            logger.debug(f"Task exception: {r}")
            continue
        clean_results.append(r)

    # Update threshold manager
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

        if not has_rates:
            logger.debug(f"Skip {s}: Missing rates (L={rl}, X={rx})")
            continue

        # Update volatility monitor
        if px is not None:
            try:
                get_volatility_monitor().update_price(s, px)
            except Exception as e:
                logger.debug(f"Volatility monitor update failed for {s}: {e}")

        net = rl - rx
        apy = abs(net) * 24 * 365

        req_apy = threshold_manager.get_threshold(s, is_maker=True)
        if apy < req_apy:
            continue

        if not has_prices:
            logger.debug(f"Skip {s}: Missing prices (X={px}, L={pl})")
            continue
        
        # CRITICAL: Guard against division by zero AND invalid prices
        try:
            if px is None or pl is None or px <= 0 or pl <= 0:
                logger.debug(f"Skip {s}: Invalid prices (X={px}, L={pl})")
                continue
            
            # Safe spread calculation with explicit float conversion
            px_float = float(px)
            pl_float = float(pl)
            
            if px_float <= 0:
                logger.debug(f"Skip {s}: X10 price is zero/negative ({px_float})")
                continue
                
            spread = abs(px_float - pl_float) / px_float
            
            if spread > config.MAX_SPREAD_FILTER_PERCENT:
                logger.debug(f"Skip {s}: Spread {spread*100:.2f}% too high")
                continue
                
        except (TypeError, ValueError, ZeroDivisionError) as e:
            logger.debug(f"Skip {s}: Price calculation error - {e}")
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
            'price_lighter': pl_float
        })

    # IMPORTANT: Set farm flag based on config (ensure farm loop state applies)
    farm_mode_active = getattr(config, 'VOLUME_FARM_MODE', False)
    for opp in opps:
        if farm_mode_active:
            opp['is_farm_trade'] = True

    logger.info(f"✅ Found {len(opps)} opportunities from {valid_pairs} valid pairs (farm_mode={farm_mode_active}, scanned={len(clean_results)})")

    opps.sort(key=lambda x: x['apy'], reverse=True)
    return opps[:config.MAX_OPEN_TRADES]

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
    reserved_amount = 0.0  # Initialize so `finally` can always reference it
    lock = await get_execution_lock(symbol)
    if lock.locked():
        return False

    async with lock:
        # Check if already open
        existing = await get_open_trades()
        if any(t['symbol'] == symbol for t in existing):
            return False

        # Volatility check
        vol_monitor = get_volatility_monitor()
        if not vol_monitor.can_enter_trade(symbol):
            return False

        # Account rotation
        acct_mgr = get_account_manager()
        acc_x10 = acct_mgr.get_next_x10_account()
        acc_lit = acct_mgr.get_next_lighter_account()
        current_label = f"{acc_x10.get('label')}/{acc_lit.get('label')}"

        # Prediction
        btc_trend = x10.get_24h_change_pct("BTC-USD")
        predictor = get_predictor()

        current_rate_lit = lighter.fetch_funding_rate(symbol) or 0.0
        current_rate_x10 = x10.fetch_funding_rate(symbol) or 0.0
        net_rate = current_rate_lit - current_rate_x10

        pred_rate, delta, conf = await predictor.predict_next_funding_rate(
            symbol=symbol,
            current_lighter_rate=current_rate_lit,
            current_x10_rate=current_rate_x10,
            lighter_adapter=lighter,      # ← Adapter-Objekt
            x10_adapter=x10,              # ← Adapter-Objekt
            btc_price=x10.fetch_mark_price("BTC-USD")
        )

        logger.debug(
            f"Predictor result for {symbol}: pred_rate={pred_rate:.6f}, delta={delta:.6f}, "
            f"conf={conf:.3f}, net_rate={net_rate:.6f}"
        )

        # Revised predictor filter: only skip when predictor gives a strong
        # negative signal or when predictor is very uncertain and the net rate
        # is negligibly small. This avoids blocking valid trades when the
        # predictor returns lower absolute values by default.
        skip_due_to_prediction = False
        if not opp.get('is_farm_trade'):
            if conf > 0.7 and delta < -0.00001:
                # Predictor is confident the rate will worsen
                logger.info(f"Skipping {symbol}: predictor predicts rate decline (delta={delta:.6f}, conf={conf:.2f})")
                skip_due_to_prediction = True
            elif conf < 0.3 and abs(net_rate) < 0.0001:
                # Predictor uncertain AND rate is too small to justify trade
                logger.debug(f"Skipping {symbol}: weak signal (conf={conf:.2f}, net={net_rate:.6f})")
                skip_due_to_prediction = True

        if skip_due_to_prediction:
            return False

        logger.debug(f"✅ Predictor OK for {symbol}: delta={delta:.6f}, conf={conf:.2f}")

        # ═══════════════════════════════════════════════════════════════
        # CRITICAL FIX: CHECK BALANCE **BEFORE** RESERVATION
        # ═══════════════════════════════════════════════════════════════

        global IN_FLIGHT_MARGIN, IN_FLIGHT_LOCK

        spread_pct = opp.get('spread_pct', 0.0)
        # Enrich logging: include leg info, signed spread, net funding, OI and prediction
        apy = opp.get('apy', 0.0)
        net_hourly = opp.get('net_funding_hourly', 0.0)
        oi = opp.get('open_interest', 0.0)
        leg1 = opp.get('leg1_exchange', 'N/A')
        side1 = opp.get('leg1_side', 'N/A')
        pred_conf = opp.get('prediction_confidence', 0.0)

        logger.info(
            f"Preparing execution for {symbol} | "
            f"leg={leg1}:{side1} | "
            f"spread={spread_pct*100:+.3f}% | APY={apy:.1f}% | "
            f"net_hr={net_hourly:+.6f} | OI=${oi:,.0f} | "
            f"acct={current_label} | pred={pred_conf:.2f}"
        )
        
        try:
            # STEP 1: GET AVAILABLE BALANCE (with lock)
            async with IN_FLIGHT_LOCK:
                if SHUTDOWN_FLAG:
                    logger.debug(f"Shutdown detected before initial balance check for {symbol}")
                    return False
                try:
                    raw_x10 = await x10.get_real_available_balance()
                    raw_lit = await lighter.get_real_available_balance()
                except Exception as e:
                    if SHUTDOWN_FLAG:
                        logger.debug(f"Balance fetch cancelled during shutdown for {symbol}")
                        return False
                    logger.error(f"Balance fetch failed for {symbol}: {e}")
                    return False

                bal_x10_real = max(0.0, raw_x10 - IN_FLIGHT_MARGIN.get('X10', 0.0))
                bal_lit_real = max(0.0, raw_lit - IN_FLIGHT_MARGIN.get('Lighter', 0.0))
            
            # STEP 2: CALCULATE SIZE
            if opp.get('is_farm_trade'):
                final_usd = config.FARM_NOTIONAL_USD  # Use config value
                logger.info(f"🚜 Farm trade {symbol}: ${final_usd}")
            else:
                # ABORT EARLY if insufficient
                if bal_x10_real < 20.0 or bal_lit_real < 20.0:
                    logger.warning(
                        f"⚠️ Insufficient balance for {symbol}: "
                        f"X10=${bal_x10_real:.1f}, Lit=${bal_lit_real:.1f}"
                    )
                    # Balance is temporarily low — do not mark as failed (no cooldown)
                    return False

                max_per_trade = min(bal_x10_real, bal_lit_real) * 0.20
                size_calc = calculate_smart_size(conf, bal_x10_real + bal_lit_real)
                size_calc *= vol_monitor.get_size_adjustment(symbol)
                final_usd = min(size_calc, max_per_trade, config.MAX_TRADE_SIZE_USD)

                logger.info(
                    f"Sizing for {symbol}: size_calc=${size_calc:.2f}, max_per_trade=${max_per_trade:.2f}, "
                    f"final_usd=${final_usd:.2f} (vol_adj={vol_monitor.get_size_adjustment(symbol):.2f})"
                )

            # Exchange minimum - NUR für non-farm trades erzwingen
            min_req = max(x10.min_notional_usd(symbol), lighter.min_notional_usd(symbol))
            
            # Für Farm-Trades: Minimum ist config.FARM_NOTIONAL_USD (nicht exchange minimum)
            if opp.get('is_farm_trade'):
                # CRITICAL FIX: Auch Farm Trades MÜSSEN das Exchange-Minimum respektieren
                # Setze final_usd mindestens auf die größere der beiden Werte:
                # - gewünschte Farm-Größe (config.FARM_NOTIONAL_USD)
                # - exchange-minimum (min_req)
                target_farm_size = float(getattr(config, 'FARM_NOTIONAL_USD', 0.0))
                final_usd = max(target_farm_size, min_req)
            else:
                # Normal trades müssen exchange minimum einhalten
                if final_usd < min_req:
                    logger.info(f"Size ${final_usd:.1f} < exchange min ${min_req:.1f}, adjusting...")
                    final_usd = float(min_req)

            # Absolute minimum (5 statt 12 für Farm trades)
            abs_min = 5.0 if opp.get('is_farm_trade') else 12.0
            if final_usd < abs_min:
                logger.debug(f"Size too small: ${final_usd:.1f} < ${abs_min:.1f}")
                return False

            # STEP 3: FINAL BALANCE CHECK BEFORE RESERVATION
            async with IN_FLIGHT_LOCK:
                # RE-CHECK after lock (race condition protection)
                if SHUTDOWN_FLAG:
                    logger.debug(f"Shutdown detected before final balance re-check for {symbol}")
                    return False
                try:
                    raw_x10 = await x10.get_real_available_balance()
                    raw_lit = await lighter.get_real_available_balance()
                except Exception as e:
                    if SHUTDOWN_FLAG:
                        logger.debug(f"Balance re-check cancelled during shutdown for {symbol}")
                        return False
                    logger.error(f"Balance re-check failed for {symbol}: {e}")
                    return False

                bal_x10_check = max(0.0, raw_x10 - IN_FLIGHT_MARGIN.get('X10', 0.0))
                bal_lit_check = max(0.0, raw_lit - IN_FLIGHT_MARGIN.get('Lighter', 0.0))
                
                # ABORT if insufficient BEFORE reservation
                if bal_x10_check < final_usd or bal_lit_check < final_usd:
                    logger.error(
                        f"🚫 ABORT {symbol}: Insufficient balance - "
                        f"X10=${bal_x10_check:.1f}, Lit=${bal_lit_check:.1f}, Need=${final_usd:.1f}"
                    )
                    # Do NOT set FAILED_COINS for balance aborts — temporary condition
                    # CRITICAL: Remove from ACTIVE_TASKS immediately
                    if symbol in ACTIVE_TASKS:
                        try:
                            del ACTIVE_TASKS[symbol]
                            logger.debug(f"🧹 Cleaned task after balance abort: {symbol}")
                        except KeyError:
                            pass
                    return False
                
                # STEP 4: NOW RESERVE (balance is confirmed sufficient)
                # Reserve with larger buffer to account for slippage/margin changes
                # ÄNDERUNG: Von 1.5 auf 1.05 gesenkt (5% statt 50% Puffer)
                reserved_amount = final_usd * 1.05 
                # We're already inside `async with IN_FLIGHT_LOCK` here, so update the shared
                # `IN_FLIGHT_MARGIN` directly to avoid re-acquiring the lock.
                IN_FLIGHT_MARGIN['X10'] = IN_FLIGHT_MARGIN.get('X10', 0.0) + reserved_amount
                IN_FLIGHT_MARGIN['Lighter'] = IN_FLIGHT_MARGIN.get('Lighter', 0.0) + reserved_amount

                logger.debug(f"🔒 Reserved ${reserved_amount:.1f} for {symbol}")

        except Exception as e:
            logger.error(f"Balance check/reservation error {symbol}: {e}")
            FAILED_COINS[symbol] = time.time()
            return False

        # SIDE MAPPING
        ex1 = x10 if opp['leg1_exchange'] == 'X10' else lighter
        ex2 = lighter if opp['leg1_exchange'] == 'X10' else x10
        side1 = opp['leg1_side']
        side2 = "SELL" if side1 == "BUY" else "BUY"

        x10_side = side1 if getattr(ex1, 'name', '') == 'X10' else side2
        lighter_side = side2 if getattr(ex1, 'name', '') == 'X10' else side1

        # PRE-FLIGHT POSITION CHECK
        try:
            x10_check = await x10.fetch_open_positions()
            lighter_check = await lighter.fetch_open_positions()
            
            if any(p['symbol'] == symbol for p in (x10_check or [])):
                logger.error(f"🚫 ABORT {symbol}: Already open on X10")
                FAILED_COINS[symbol] = time.time()
                return False
            
            if any(p['symbol'] == symbol for p in (lighter_check or [])):
                logger.error(f"🚫 ABORT {symbol}: Already open on Lighter")
                FAILED_COINS[symbol] = time.time()
                return False

        except Exception as e:
            logger.warning(f"Pre-flight check error {symbol}: {e}")

        # EXECUTE PARALLEL
        try:
            # EXECUTE PARALLEL
            try:
                # CRITICAL: Snapshot prices BEFORE trade execution (Adapter returns order_id, not price)
                est_px_x10 = x10.fetch_mark_price(symbol) or 0.0
                est_px_lit = lighter.fetch_mark_price(symbol) or 0.0

                logger.info(f"🚀 Opening {symbol}: Size=${final_usd:.1f}, X10={x10_side}, Lit={lighter_side}")
                logger.debug(f"Price snapshot: X10={est_px_x10:.6f}, Lighter={est_px_lit:.6f}")

                # ✅ CRITICAL: Fetch FRESH balance immediately before execution
                logger.info("🔍 Fetching fresh balance before execution...")
                if SHUTDOWN_FLAG:
                    logger.debug(f"Shutdown detected before fresh balance fetch for {symbol}")
                    return False
                try:
                    fresh_x10 = await x10.get_real_available_balance()
                    fresh_lit = await lighter.get_real_available_balance()
                except Exception as e:
                    if SHUTDOWN_FLAG:
                        logger.debug(f"Balance fetch cancelled during shutdown for {symbol}")
                        return False
                    logger.error(f"Balance fetch failed for {symbol}: {e}")
                    return False

                logger.info(f"💰 Fresh Balance: X10=${fresh_x10:.2f}, Lighter=${fresh_lit:.2f}")

                # Calculate required with buffers
                required_x10 = final_usd * 1.05   # 5% buffer for X10
                # ÄNDERUNG: Von 1.20 auf 1.05 gesenkt
                required_lit = final_usd * 1.05   # 5% buffer for Lighter (was 20%)

                # ═══════════════════════════════════════════════════════════════
                # CRITICAL: Log ACTUAL vs REQUIRED balance clearly (helps debugging reserved capital)
                # ═══════════════════════════════════════════════════════════════
                logger.debug(
                    f"💰 Balance check {symbol}: "
                    f"X10=${fresh_x10:.1f} (need=${required_x10:.1f}), "
                    f"Lit=${fresh_lit:.1f} (need=${required_lit:.1f})"
                )

                # Strict pre-flight check (combined — no cooldown on balance abort)
                if fresh_x10 < required_x10 or fresh_lit < required_lit:
                    logger.error(
                        f"🚫 ABORT {symbol}: Insufficient balance - "
                        f"X10=${fresh_x10:.1f}, Lit=${fresh_lit:.1f}, Need=${max(required_x10, required_lit):.1f}"
                    )
                    # KEIN FAILED_COINS[symbol] setzen bei Balance-Problem!
                    return False

                logger.info(f"✅ Balance OK: X10=${fresh_x10:.2f} >= ${required_x10:.2f}, Lit=${fresh_lit:.2f} >= ${required_lit:.2f}")

                logger.debug(
                    f"Execution details for {symbol}: final_usd=${final_usd:.2f}, required_x10=${required_x10:.2f}, "
                    f"required_lit=${required_lit:.2f}, x10_side={x10_side}, lit_side={lighter_side}"
                )

                # ═══════════════════════════════════════════════════════════════
                # CRITICAL: Use ParallelExecutionManager für Pre-Hedge + Rollback
                # ═══════════════════════════════════════════════════════════════
                logger.info(f"🚀 Opening {symbol}: PARALLEL execution via ParallelExecutionManager")

                # Normalize leg vars for ParallelExecutionManager
                leg1_side = opp.get('leg1_side', 'BUY')
                leg1_exchange = opp.get('leg1_exchange', 'X10')
                opposite_side = "SELL" if leg1_side == "BUY" else "BUY"

                success, x10_order_id, lit_order_id = await parallel_exec.execute_trade_parallel(
                    symbol=symbol,
                    side_x10=leg1_side if leg1_exchange == "X10" else opposite_side,
                    side_lighter=leg1_side if leg1_exchange == "Lighter" else opposite_side,
                    size_x10=final_usd,
                    size_lighter=final_usd,
                    price_x10=None,
                    price_lighter=None
                )
                
                if not success:
                    logger.error(f"❌ Parallel execution failed for {symbol}")
                    FAILED_COINS[symbol] = time.time()
                    return False

                # On success: store trade snapshot
                is_farm = opp.get('is_farm_trade', False)
                trade_data = {
                    'symbol': symbol,
                    'entry_time': datetime.now(timezone.utc),
                    'notional_usd': final_usd,
                    'status': 'OPEN',
                    'leg1_exchange': opp['leg1_exchange'],
                    'initial_spread_pct': opp.get('spread_pct', 0.0),
                    'initial_funding_rate_hourly': abs(net_rate),
                    'entry_price_x10': float(est_px_x10),
                    'entry_price_lighter': float(est_px_lit),
                    'is_farm_trade': is_farm,
                    'account_label': current_label
                }
                state_mgr = get_state_manager()
                await state_mgr.add_trade(trade_data)
                return True
                
            except Exception as e:
                logger.error(f"Execution error {symbol}: {e}")
                import traceback
                traceback.print_exc()
                
                # CLEANUP any partial fills (wait longer for positions to settle)
                await asyncio.sleep(10.0)
                
                cleanup_x10 = await x10.fetch_open_positions()
                cleanup_lit = await lighter.fetch_open_positions()
                
                x10_ghost = any(p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 for p in (cleanup_x10 or []))
                lit_ghost = any(p['symbol'] == symbol and abs(p.get('size', 0)) > 1e-8 for p in (cleanup_lit or []))
                
                if x10_ghost:
                    logger.error(f"🧟 Cleaning X10 ghost for {symbol}")
                    await safe_close_x10_position(x10, symbol, x10_side, final_usd)
                
                if lit_ghost:
                    logger.error(f"🧟 Cleaning Lighter ghost for {symbol}")
                    await lighter.close_live_position(symbol, lighter_side, final_usd)
                
                FAILED_COINS[symbol] = time.time()
                return False

        finally:
            # ═══════════════════════════════════════════════════════════════
            # CRITICAL: ALWAYS RELEASE MARGIN ON **ANY** EXIT PATH
            # ═══════════════════════════════════════════════════════════════
            if reserved_amount > 0.01:
                async with IN_FLIGHT_LOCK:
                    IN_FLIGHT_MARGIN['X10'] = max(0.0, IN_FLIGHT_MARGIN.get('X10', 0.0) - reserved_amount)
                    IN_FLIGHT_MARGIN['Lighter'] = max(0.0, IN_FLIGHT_MARGIN.get('Lighter', 0.0) - reserved_amount)
                    
                    logger.info(
                        f"🔓 Released ${reserved_amount:.1f} for {symbol} | "
                        f"Remaining: X10=${IN_FLIGHT_MARGIN['X10']:.1f}, Lit=${IN_FLIGHT_MARGIN['Lighter']:.1f}"
                    )

async def close_trade(trade: Dict, lighter, x10) -> bool:
    symbol = trade['symbol']
    notional = trade['notional_usd']
    
    # Simple logic: Just close both sides optimistically
    logger.info(f" 🔻 CLOSING {symbol}...")
    res = await asyncio.gather(
        x10.close_live_position(symbol, "BUY", notional), # Side parameter ignored by intelligent close logic usually
        lighter.close_live_position(symbol, "BUY", notional),
        return_exceptions=True
    )
    
    # Check success (tuple returns (bool, order_id))
    s1 = res[0][0] if isinstance(res[0], tuple) else False
    s2 = res[1][0] if isinstance(res[1], tuple) else False
    
    if s1 and s2:
        return True
    
    logger.warning(f" Close partial fail {symbol}: X10={s1}, Lit={s2}")
    return False # Retry logic handles this

async def get_actual_position_size(adapter, symbol: str) -> Optional[float]:
    """Get actual position size in coins from exchange"""
    try:
        positions = await adapter.fetch_open_positions()
        for p in (positions or []):
            if p.get('symbol') == symbol:
                return p.get('size', 0)
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
        
        # Close with ACTUAL coin size
        price = x10.fetch_mark_price(symbol)
        if not price:
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

    for t in trades:
        sym = t['symbol']
        px, pl = x10.fetch_mark_price(sym), lighter.fetch_mark_price(sym)
        if not px or not pl: continue

        rx = x10.fetch_funding_rate(sym) or 0
        rl = lighter.fetch_funding_rate(sym) or 0
        # Improved Net Calc
        base_net = rl - rx
        current_net = -base_net if t['leg1_exchange'] == 'X10' else base_net

        # PnL & Duration
        entry_time = t['entry_time']
        if isinstance(entry_time, str):
            try: entry_time = datetime.fromisoformat(entry_time)
            except: entry_time = datetime.utcnow()

        hold_hours = (datetime.utcnow() - entry_time).total_seconds() / 3600
        funding_pnl = current_net * hold_hours * t['notional_usd']

        # Spread
        entry_spread = abs(t['entry_price_x10'] - t['entry_price_lighter'])
        curr_spread = abs(px - pl)
        spread_pnl = (entry_spread - curr_spread) / px * t['notional_usd']

        # Fees (Dynamic from State Manager)
        fee_x10 = state_manager.get_fee_estimate('X10', sym, default=config.TAKER_FEE_X10)
        fee_lit = state_manager.get_fee_estimate('Lighter', sym, default=0.0)
        est_fees = t['notional_usd'] * (fee_x10 + fee_lit) * 2.0

        total_pnl = funding_pnl + spread_pnl - est_fees

        # Check Exits
        reason = None
        
        # 1. Farm Check (Priorität!)
        # Zuerst prüfen, damit Farm-Trades sicher geschlossen werden
        if not reason and t.get('is_farm_trade'):
            # Farm Hold Time aus Config oder Default 60s
            limit_seconds = getattr(config, 'FARM_HOLD_SECONDS', 60)
            if hold_hours * 3600 > limit_seconds:
                reason = "FARM_COMPLETE"
                logger.info(f"🚜 EXIT FARM {sym}: Hold={hold_hours*60:.1f}min > {limit_seconds}s")

        # 2. Volatility Panic (in try-block geschützt)
        if not reason:
            try:
                vol_mon = get_volatility_monitor()
                # Prüfen ob Methode existiert
                if hasattr(vol_mon, 'should_close_due_to'):
                    # Volatility sicher abrufen
                    vol = 0.0
                    if hasattr(vol_mon, 'get_volatility'):
                        vol = vol_mon.get_volatility(sym)
                    
                    if vol_mon.should_close_due_to(vol):
                        reason = "VOLATILITY_PANIC"
            except Exception as e:
                logger.warning(f"VolCheck Error {sym}: {e}")

        # 3. Stop Loss / Take Profit
        if not reason:
            if total_pnl < -t['notional_usd'] * 0.03:
                reason = "STOP_LOSS"
            elif total_pnl > t['notional_usd'] * 0.05:
                reason = "TAKE_PROFIT"
            elif t.get('initial_funding_rate_hourly', 0) * current_net < 0:
                # ... bestehende Funding Flip Logik ...
                if not t.get('funding_flip_start_time'):
                    t['funding_flip_start_time'] = datetime.utcnow()
                    if state_manager:
                        await state_manager.update_trade(sym, {'funding_flip_start_time': datetime.utcnow()})
                else:
                    flip_start = t['funding_flip_start_time']
                    if isinstance(flip_start, str): flip_start = datetime.fromisoformat(flip_start)
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
                telegram = get_telegram_bot()
                if telegram.enabled:
                    await telegram.send_trade_alert(sym, reason, t['notional_usd'], total_pnl)

async def cleanup_zombie_positions(lighter, x10):
    """Full Zombie Cleanup Implementation with Correct Side Detection"""
    try:
        x_pos, l_pos = await get_cached_positions(lighter, x10, force=True)

        x_syms = {p['symbol'] for p in x_pos if abs(p.get('size', 0)) > 1e-8}
        l_syms = {p['symbol'] for p in l_pos if abs(p.get('size', 0)) > 1e-8}

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
                        position_size = p.get('size', 0)

                        # CRITICAL FIX: Determine close side based on CURRENT position
                        # Positive size = LONG position → close with SELL
                        # Negative size = SHORT position → close with BUY
                        if position_size > 0:
                            close_side = "SELL"  # Close LONG
                            original_side = "BUY"
                        else:
                            close_side = "BUY"   # Close SHORT
                            original_side = "SELL"

                        size_usd = abs(position_size) * (x10.fetch_mark_price(sym) or 0.0)

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
                        except Exception as e:
                            logger.error(f"X10 zombie close exception for {sym}: {e}")

                # Lighter Zombie Cleanup
                if sym in l_syms:
                    p = next((pos for pos in l_pos if pos['symbol'] == sym), None)
                    if p:
                        position_size = p.get('size', 0)

                        # CRITICAL FIX: Same logic for Lighter
                        if position_size > 0:
                            close_side = "SELL"
                            original_side = "BUY"
                        else:
                            close_side = "BUY"
                            original_side = "SELL"

                        size_usd = abs(position_size) * (lighter.fetch_mark_price(sym) or 0.0)

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

            # Check concurrency limit
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
                
                px = x10.fetch_mark_price(sym)
                pl = lighter.fetch_mark_price(sym)
                
                if not px or not pl:
                    continue
                
                spread = abs(px - pl) / px if px else 1.0
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
    REFRESH_DELAY = getattr(config, 'REFRESH_DELAY_SECONDS', 5)
    logger.info(f"Logic Loop gestartet – REFRESH alle {REFRESH_DELAY}s")

    while not SHUTDOWN_FLAG:
        try:
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
                
                executed_count = 0
                max_per_cycle = 3
                
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

                # KRITISCH: Bot NICHT stoppen wenn Balance-Erkennung fehlschlägt!
                # Stattdessen: Warning + weiter versuchen
                if bal_x10 < 0.01:
                    logger.warning(
                        "⚠️ X10 Balance = $0 (API-Problem, NICHT echte Balance!) "
                        "→ Bot läuft weiter, Balance-Check wird übersprungen"
                    )
                    # Setze einen Fallback-Wert basierend auf historischen Daten oder Skip
                    bal_x10 = 50.0  # Annahme: Es gibt Balance, API kann sie nur nicht lesen
                    
                elif bal_x10 < 5.0:
                    logger.warning(
                        f"⚠️ X10 Balance niedrig (${bal_x10:.2f}) aber Bot läuft weiter. "
                        "Neue Trades werden pausiert bis Balance steigt."
                    )
                    # KEIN return! Bot läuft weiter, aber keine neuen Trades

                open_trades_count = len(open_trades)
                
                # ═══════════════════════════════════════════════════════════════
                # CRITICAL: Only reserve capital for ACTUAL trades
                # Not for ghost DB entries
                # ═══════════════════════════════════════════════════════════════
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
                    actual_open_count = open_trades_count  # Fallback
                
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
                    f"DB_Trades={open_trades_count}, Real_Positions={actual_open_count}, "
                    f"Locked=${locked_per_exchange:.0f}/ex"
                )
                
                if available_x10 < avg_trade_size or available_lit < avg_trade_size:
                    if available_x10 < 0 or available_lit < 0:
                        logger.warning(
                            f"⚠️  NEGATIVE available balance detected! "
                            f"This indicates ghost trades or stale DB entries."
                        )
                    logger.debug(
                        f"⏸️  Paused: Insufficient available balance "
                        f"(need ${avg_trade_size:.0f}, have X10=${available_x10:.1f}, Lit=${available_lit:.1f})"
                    )
                    await asyncio.sleep(REFRESH_DELAY * 3)
                    continue
                
                for opp in opportunities:
                    if executed_count >= max_per_cycle:
                        break
                        
                    symbol = opp['symbol']
                    
                    if symbol in ACTIVE_TASKS:
                        continue
                    
                    if symbol in FAILED_COINS and (time.time() - FAILED_COINS[symbol] < 180):
                        continue
                    
                    if symbol in open_syms:
                        continue
                    
                    # ═══════════════════════════════════════════════════════════════
                    # FIX: Check balance BEFORE launching task — use opportunity's
                    # recommended notional when available, otherwise fall back
                    # to farm/desired config defaults.
                    is_farm = opp.get('is_farm_trade', False)
                    base_default = getattr(config, 'FARM_NOTIONAL_USD', 12) if is_farm else getattr(config, 'DESIRED_NOTIONAL_USD', 16)

                    # Prefer an explicit size provided by the opportunity payload
                    trade_size = None
                    for key in ('trade_size', 'notional_usd', 'trade_notional_usd', 'desired_notional_usd'):
                        if key in opp and isinstance(opp[key], (int, float)):
                            trade_size = float(opp[key])
                            break

                    if trade_size is None:
                        trade_size = float(base_default)

                    required_margin = trade_size * 1.2  # 20% buffer
                    
                    if available_x10 < required_margin or available_lit < required_margin:
                        logger.debug(f"Skip {symbol}: Insufficient pre-check balance (X10=${available_x10:.1f}, Lit=${available_lit:.1f}, Need=${required_margin:.1f})")
                        continue
                    
                    # Deduct from available balance for next iteration
                    available_x10 -= required_margin
                    available_lit -= required_margin
                    
                    logger.info(f"🚀 Launching trade for {symbol} (APY={opp['apy']:.1f}%)")

                    # ═══════════════════════════════════════════════════════════════════════════════
                    # CRITICAL: Symbol-Level Lock verhindert doppelte Trades
                    # ═══════════════════════════════════════════════════════════════════════════════
                    async with TASKS_LOCK:
                        if symbol in ACTIVE_TASKS:
                            logger.debug(f"⏸️  {symbol} already has active task, skipping")
                            continue

                    lock = get_symbol_lock(symbol)

                    async def _handle_with_lock():
                        async with lock:
                            try:
                                # Directly perform the execution routine for the opportunity
                                await execute_trade_parallel(opp, lighter, x10, parallel_exec)
                            finally:
                                async with TASKS_LOCK:
                                    ACTIVE_TASKS.pop(symbol, None)

                    # CRITICAL: Don't start new tasks during shutdown
                    if SHUTDOWN_FLAG:
                        logger.debug(f"Shutdown active, skipping task for {symbol}")
                        continue

                    task = asyncio.create_task(_handle_with_lock())
                    async with TASKS_LOCK:
                        if SHUTDOWN_FLAG:
                            task.cancel()
                            continue
                        ACTIVE_TASKS[symbol] = task

                    executed_count += 1
                    
                    await asyncio.sleep(0.5)
                
                if executed_count > 0:
                    logger.info(f"✅ Launched {executed_count} trade tasks this cycle")
            
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
    """Background tasks: Funding rates refresh + REST Fallback für BEIDE Exchanges"""
    funding_refresh_interval = 30  # Alle 30s Funding Rates refreshen
    last_x10_refresh = 0
    last_lighter_refresh = 0
    
    while True:
        try:
            now = time.time()
            
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
            
            await asyncio.sleep(30)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Maintenance loop error: {e}")
            await asyncio.sleep(30)

async def setup_database():
    """Ensure Tables Exist"""
    async with aiosqlite.connect(config.DB_FILE) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                symbol TEXT PRIMARY KEY,
                entry_time TIMESTAMP,
                notional_usd REAL,
                status TEXT DEFAULT 'OPEN',
                leg1_exchange TEXT,
                initial_spread_pct REAL,
                initial_funding_rate_hourly REAL,
                entry_price_x10 REAL,
                entry_price_lighter REAL,
                funding_flip_start_time TIMESTAMP,
                is_farm_trade BOOLEAN DEFAULT FALSE,
                account_label TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                entry_time TIMESTAMP,
                exit_time TIMESTAMP,
                hold_duration_hours REAL,
                close_reason TEXT,
                final_pnl_usd REAL,
                funding_pnl_usd REAL,
                spread_pnl_usd REAL,
                fees_usd REAL,
                account_label TEXT
            )
        """)
        # Fee table also ensured here (defensive - load_fee_cache also creates it)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fee_stats (
                exchange TEXT, symbol TEXT, 
                avg_fee_rate REAL, sample_size INTEGER, 
                last_updated TIMESTAMP,
                PRIMARY KEY (exchange, symbol)
            )
        """)
        await conn.commit()
#
async def migrate_database():
    """Migrate DB schema for new columns"""
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            # Check if migration needed
            cursor = await conn.execute("PRAGMA table_info(trade_history)")
            columns = await cursor.fetchall()
            has_account_label = any(col[1] == 'account_label' for col in columns)
            
            if not has_account_label:
                logger.info("🔄 Migrating database schema...")
                await conn.execute("ALTER TABLE trade_history ADD COLUMN account_label TEXT DEFAULT 'Main'")
                await conn.execute("ALTER TABLE trades ADD COLUMN account_label TEXT DEFAULT 'Main'")
                await conn.commit()
                logger.info("✅ Database migration complete")
            else:
                logger.debug("✅ Database schema up to date")
                
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        logger.warning("⚠️ Deleting old database and recreating...")
        import os
        if os.path.exists(config.DB_FILE):
            os.remove(config.DB_FILE)
        await setup_database()

# ============================================================
# MAIN
# ============================================================
async def main():
    global SHUTDOWN_FLAG, state_manager
    logger.info(" 🔥 BOT V4 (Full Architected) STARTING...")

    # 1. Init Infrastructure
    state_manager = InMemoryStateManager(config.DB_FILE)
    await state_manager.start()  # ← KRITISCH: Background Writer starten
    logger.info("✅ State Manager background writer started")
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
        # PARALLEL statt sequential: gather with return_exceptions to inspect errors
        load_results = await asyncio.gather(
            x10.load_market_cache(force=True),
            lighter.load_market_cache(force=True),
            return_exceptions=True
        )

        # Check for errors from the parallel load
        for i, result in enumerate(load_results):
            if isinstance(result, Exception):
                logger.error(f"Market load failed: {result}")

        x10_count = len(x10.market_info)
        lit_count = len(lighter.market_info)

        logger.info(f"✅ Markets loaded: X10={x10_count}, Lighter={lit_count}")

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
                px = x10.fetch_mark_price(sym)
                pl = lighter.fetch_mark_price(sym)
                if px and pl and px > 0 and pl > 0:
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
    ws_manager = WebSocketManager([x10, lighter])
    # Prepare cleanup task variable to avoid UnboundLocalError in finally
    cleanup_task = None
    await ws_manager.start()

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
        # ═══════════════════════════════════════════════════════════════
        # CRITICAL: SHUTDOWN_FLAG ZUERST setzen!
        # ═══════════════════════════════════════════════════════════════
        SHUTDOWN_FLAG = True
        logger.info("SHUTDOWN_FLAG gesetzt - keine neuen Trades mehr")
        
        # Warte kurz damit alle Loops das Flag sehen
        await asyncio.sleep(0.5)

        # ═══════════════════════════════════════════════════════════════
        # STEP 1: Cancel ALLE Haupt-Tasks ZUERST
        # ═══════════════════════════════════════════════════════════════
        logger.info("Cancelling main tasks...")
        for task in tasks:
            if not task.done():
                task.cancel()
        
        # Warte auf alle Haupt-Tasks
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, asyncio.CancelledError):
                    logger.debug(f"Task {tasks[i].get_name()} cancelled OK")
                elif isinstance(result, Exception):
                    logger.error(f"Task {tasks[i].get_name()} error: {result}")

        # ═══════════════════════════════════════════════════════════════
        # STEP 2: Cancel ALLE Symbol-Tasks
        # ═══════════════════════════════════════════════════════════════
        logger.info("Cancelling symbol tasks...")
        async with TASKS_LOCK:
            for sym, task in list(ACTIVE_TASKS.items()):
                if not task.done():
                    task.cancel()
            if ACTIVE_TASKS:
                await asyncio.gather(*ACTIVE_TASKS.values(), return_exceptions=True)
            ACTIVE_TASKS.clear()
            SYMBOL_LOCKS.clear()

        # ═══════════════════════════════════════════════════════════════
        # STEP 2.5: Clear IN_FLIGHT_MARGIN to prevent leaks
        # ═══════════════════════════════════════════════════════════════
        async with IN_FLIGHT_LOCK:
            old_x10 = IN_FLIGHT_MARGIN.get('X10', 0)
            old_lit = IN_FLIGHT_MARGIN.get('Lighter', 0)
            if old_x10 > 0 or old_lit > 0:
                logger.info(f"🔓 Clearing IN_FLIGHT_MARGIN: X10=${old_x10:.1f}, Lit=${old_lit:.1f}")
            IN_FLIGHT_MARGIN.clear()
        # ═══════════════════════════════════════════════════════════════
        # STEP 3: Stoppe Manager (WebSocket, State, Telegram)
        # ═══════════════════════════════════════════════════════════════
        logger.info("Stopping Managers...")
        await ws_manager.stop()

        # Cancel the cleanup task started earlier (Zeile 2017)
        if cleanup_task and not cleanup_task.done():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

        # Cancel emergency cleanup
        if 'emergency_task' in locals() and not emergency_task.done():
            emergency_task.cancel()
            try:
                await emergency_task
            except asyncio.CancelledError:
                pass

        await state_manager.stop()

        if telegram_bot and telegram_bot.enabled:
            await telegram_bot.send_message("Bot Shutdown")
            await telegram_bot.stop()

        # ═══════════════════════════════════════════════════════════════
        # STEP 4: Schließe Adapter
        # ═══════════════════════════════════════════════════════════════
        logger.info("Closing adapters...")
        try:
            await x10.aclose()
        except Exception as e:
            logger.error(f"Error closing X10: {e}")

        try:
            await lighter.aclose()
        except Exception as e:
            logger.error(f"Error closing Lighter: {e}")

        # Kurz warten damit aiohttp Sessions sauber schließen
        await asyncio.sleep(0.5)

        logger.info("BOT SHUTDOWN COMPLETE.")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass