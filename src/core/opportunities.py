# src/core/opportunities.py
"""
Opportunity detection for funding arbitrage.

This module handles:
- Finding trading opportunities across exchanges
- Latency arbitrage detection
- Profitability calculations
- Filtering (blacklist, volatility, spread limits)
"""

import asyncio
import logging
import time
from typing import List, Dict, Optional, Any
from decimal import Decimal

import config
from src.utils import safe_float, safe_decimal, quantize_usd
from src.adaptive_threshold import get_threshold_manager
from src.volatility_monitor import get_volatility_monitor
from src.latency_arb import get_detector, is_latency_arb_enabled

logger = logging.getLogger(__name__)

# ============================================================
# GLOBALS
# ============================================================
FAILED_COINS = {}
OPPORTUNITY_LOG_CACHE = {}


# ============================================================
# HELPER FUNCTIONS
# ============================================================
def is_tradfi_or_fx(symbol: str) -> bool:
    """Check if symbol is TradFi or FX (excluded from trading)"""
    s = symbol.upper().replace("-USD", "").replace("/", "")
    if s.startswith(("XAU", "XAG", "XBR", "WTI", "PAXG")): 
        return True
    if s.startswith(("EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "TRY")) and "EUROC" not in s: 
        return True
    if s.startswith(("SPX", "NDX", "US30", "DJI", "NAS")): 
        return True
    return False


def calculate_expected_profit(
    notional_usd: float,
    hourly_funding_rate: float,
    hold_hours: float,
    spread_pct: float,
    fee_rate: float = None
) -> tuple:
    """
    Calculate expected profit and hours to breakeven for a trade.
    
    ⚡ KRITISCH: Verhindert Trades die nie profitabel werden!
    
    Uses Decimal internally for precision, returns float for compatibility.
    
    Args:
        notional_usd: Trade size in USD
        hourly_funding_rate: Net funding rate per hour (|lighter_rate - x10_rate|)
        hold_hours: Expected hold duration in hours
        spread_pct: Current spread as decimal (e.g., 0.003 for 0.3%)
        fee_rate: Taker fee rate (default: X10 taker from config)
        
    Returns:
        (expected_profit_usd: float, hours_to_breakeven: float)
    """
    if fee_rate is None:
        fee_rate = config.TAKER_FEE_X10
        
    # Convert all inputs to Decimal for precision
    notional = safe_decimal(notional_usd)
    rate = safe_decimal(abs(hourly_funding_rate))
    hours = safe_decimal(hold_hours)
    spread = safe_decimal(spread_pct)
    fee = safe_decimal(fee_rate)
    
    # Expected funding income over hold period
    funding_income = rate * hours * notional
    
    # Entry + Exit fees on both exchanges
    # X10: Taker both ways (entry + exit) = fee_rate * 2
    # Lighter: Maker = 0% (free)
    total_fees = notional * fee * Decimal('2')  # Only X10 fees
    
    # Spread slippage cost (estimated as half the spread on entry)
    slippage_cost = notional * spread * Decimal('0.5')
    
    # Total cost
    total_cost = total_fees + slippage_cost
    
    # Expected profit
    expected_profit = funding_income - total_cost
    
    # Hours to breakeven (how long to hold to cover costs)
    hourly_income = rate * notional
    if hourly_income > Decimal('0'):
        hours_to_breakeven = total_cost / hourly_income
    else:
        hours_to_breakeven = Decimal('999999')  # Never profitable
    
    # Return as float for compatibility with existing code
    return float(quantize_usd(expected_profit)), float(hours_to_breakeven)


# ============================================================
# MAIN OPPORTUNITY FINDER
# ============================================================
async def find_opportunities(lighter, x10, open_syms, is_farm_mode: bool = None) -> List[Dict]:
    """
    Find trading opportunities across Lighter and X10.

    Args:
        lighter: Lighter adapter
        x10: X10 adapter
        open_syms: Set of already open symbols
        is_farm_mode: If True, mark all trades as farm trades. If None, auto-detect from config.
    
    Returns:
        List of opportunity dictionaries sorted by APY
    """
    # Auto-detect farm mode if not specified
    if is_farm_mode is None:
        is_farm_mode = config.VOLUME_FARM_MODE

    opps: List[Dict] = []
    common = set(lighter.market_info.keys()) & set(x10.market_info.keys())
    threshold_manager = get_threshold_manager()
    detector = get_detector()  # ⚡ Latency Detector Instance

    # Verify market data is loaded
    if not common:
        logger.warning("⚠️ No common markets found")
        logger.debug(f"X10 markets: {len(x10.market_info)}, Lighter: {len(lighter.market_info)}")
        return []

    # Check price cache status
    x10_prices = len(x10.price_cache)
    lit_prices = len(lighter.price_cache)
    logger.debug(f"Price cache status: X10={x10_prices}/{len(common)}, Lighter={lit_prices}/{len(common)}")
    
    if x10_prices == 0 and lit_prices == 0:
        logger.warning("⚠️ Price cache completely empty - WebSocket streams may not be working")
        await asyncio.gather(
            x10.load_market_cache(force=True),
            lighter.load_market_cache(force=True),
            lighter.load_funding_rates_and_prices(),
            return_exceptions=True
        )

    logger.debug(
        f"🔍 Scanning {len(common)} pairs. "
        f"Lighter markets: {len(lighter.market_info)}, X10 markets: {len(x10.market_info)}"
    )

    semaphore = asyncio.Semaphore(10)

    async def fetch_symbol_data(s: str):
        async with semaphore:
            try:
                await asyncio.sleep(0.05)
                
                # Get funding rates (from cache)
                lr = lighter.fetch_funding_rate(s)
                xr = x10.fetch_funding_rate(s)
                
                # Get prices (from cache)
                px = x10.fetch_mark_price(s)
                pl = lighter.fetch_mark_price(s)
                
                return (s, lr, xr, px, pl)
                
            except Exception as e:
                logger.debug(f"Error fetching {s}: {e}")
                return (s, None, None, None, None)

    # Launch concurrent fetches
    tasks = [asyncio.create_task(fetch_symbol_data(s)) for s in common]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out exceptions
    clean_results = []
    for r in results:
        if isinstance(r, Exception):
            continue
        clean_results.append(r)

    # ═══════════════════════════════════════════════════════════════
    # LATENCY ARB: FIRST PRIORITY
    # ═══════════════════════════════════════════════════════════════
    latency_opportunities = []
    
    if is_latency_arb_enabled():
        for s, rl, rx, px, pl in clean_results:
            if s in open_syms or rl is None or rx is None:
                continue
            
            try:
                latency_opp = await detector.detect_lag_opportunity(
                    symbol=s,
                    x10_rate=float(rx),
                    lighter_rate=float(rl),
                    x10_adapter=x10,
                    lighter_adapter=lighter
                )
                
                if latency_opp:
                    latency_opp['price_x10'] = safe_float(px)
                    latency_opp['price_lighter'] = safe_float(pl)
                    latency_opp['spread_pct'] = abs(safe_float(px) - safe_float(pl)) / safe_float(px) if px else 0
                    
                    logger.info(
                        f"⚡ LATENCY ARB DETECTED: {s} | "
                        f"Lag={latency_opp.get('lag_seconds', 0):.2f}s | "
                        f"Confidence={latency_opp.get('confidence', 0):.2f}"
                    )
                    
                    latency_opportunities.append(latency_opp)
                    
            except Exception as e:
                logger.debug(f"Latency check error for {s}: {e}")
        
        if latency_opportunities:
            latency_opportunities.sort(
                key=lambda x: x.get('confidence', 0) * x.get('lag_seconds', 0),
                reverse=True
            )
            logger.info(f"⚡ FAST LANE: {len(latency_opportunities)} Latency Arb opportunities!")
            return latency_opportunities[:1]

    # ═══════════════════════════════════════════════════════════════
    # STANDARD FUNDING ARBITRAGE
    # ═══════════════════════════════════════════════════════════════
    current_rates = [lr for (_s, lr, _xr, _px, _pl) in clean_results if lr is not None]
    if current_rates:
        try:
            threshold_manager.update_metrics(current_rates)
        except Exception:
            pass

    now_ts = time.time()
    valid_pairs = 0
    
    # Rejection counters
    rejected_open = 0
    rejected_blacklist = 0
    rejected_tradfi = 0
    rejected_cooldown = 0
    rejected_volatility = 0
    rejected_data = 0
    rejected_spread = 0
    rejected_apy = 0
    rejected_profit = 0
    rejected_breakeven = 0

    for s, rl, rx, px, pl in clean_results:
        # Skip already open
        if s in open_syms:
            rejected_open += 1
            continue
            
        # Skip blacklisted
        if s in config.BLACKLIST_SYMBOLS:
            rejected_blacklist += 1
            continue
            
        # Skip TradFi/FX
        if is_tradfi_or_fx(s):
            rejected_tradfi += 1
            continue

        # Skip failed coins in cooldown
        if s in FAILED_COINS and (now_ts - FAILED_COINS[s] < 60):
            rejected_cooldown += 1
            continue

        # Volatility filter
        try:
            vol_monitor = get_volatility_monitor()
            vol_24h = vol_monitor.get_volatility_24h(s)
            max_vol = getattr(config, 'MAX_VOLATILITY_PCT_24H', 50.0)
            if vol_24h > max_vol:
                rejected_volatility += 1
                continue
        except Exception:
            pass

        # Validate data
        has_rates = rl is not None and rx is not None
        has_prices = px is not None and pl is not None
        
        if has_rates and has_prices:
            valid_pairs += 1
        else:
            rejected_data += 1
            continue

        # Price parsing
        try:
            px_float = safe_float(px)
            pl_float = safe_float(pl)
            if px_float <= 0 or pl_float <= 0:
                rejected_data += 1
                continue
            spread = abs(px_float - pl_float) / px_float
        except:
            rejected_data += 1
            continue

        # Calculate funding metrics
        net = rl - rx
        apy = abs(net) * 24 * 365

        req_apy = threshold_manager.get_threshold(s, is_maker=True)
        if apy < req_apy:
            # logger.debug(f"🚫 {s}: APY {apy*100:.2f}% < Min {req_apy*100:.2f}%")
            rejected_apy += 1
            continue

        # Dynamic spread check
        base_spread_limit = config.MAX_SPREAD_FILTER_PERCENT
        funding_boosted_limit = abs(net) * 12.0
        final_spread_limit = min(max(base_spread_limit, funding_boosted_limit), 0.03)
        
        if spread > final_spread_limit:
            # logger.debug(f"🚫 {s}: Spread {spread*100:.2f}% > Limit {final_spread_limit*100:.2f}%")
            rejected_spread += 1
            continue

        # Profitability check
        notional = getattr(config, 'DESIRED_NOTIONAL_USD', 500)
        farm_mode = getattr(config, 'VOLUME_FARM_MODE', False)
        
        if farm_mode:
            hold_hours = getattr(config, 'FARM_HOLD_SECONDS', 3600) / 3600
            max_breakeven = getattr(config, 'MAX_BREAKEVEN_HOURS', 2.0)
        else:
            hold_hours = 24.0
            max_breakeven = 48.0

        min_profit = getattr(config, 'MIN_PROFIT_EXIT_USD', 0.02)
        
        expected_profit, hours_to_breakeven = calculate_expected_profit(
            notional_usd=notional,
            hourly_funding_rate=abs(net),
            hold_hours=hold_hours,
            spread_pct=spread
        )
        
        min_threshold = 0.0 if farm_mode else min_profit
        
        if expected_profit < min_threshold:
            # logger.debug(f"🚫 {s}: Profit ${expected_profit:.4f} < ${min_threshold} (Cost: Spread+Fees)")
            rejected_profit += 1
            continue
        
        max_be = hold_hours if farm_mode else max_breakeven
        if hours_to_breakeven > max_be:
            rejected_breakeven += 1
            continue
        
        # ✅ Trade is profitable!
        logger.info(
            f"✅ {s}: Expected profit ${expected_profit:.4f} in {hold_hours:.1f}h "
            f"(breakeven: {hours_to_breakeven:.2f}h, APY: {apy*100:.1f}%)"
        )

        opps.append({
            'symbol': s,
            'apy': apy * 100,
            'net_funding_hourly': net,
            'leg1_exchange': 'Lighter' if rl > rx else 'X10',
            'leg1_side': 'SELL' if rl > rx else 'BUY',
            'is_farm_trade': is_farm_mode,
            'spread_pct': spread,
            'price_x10': px_float,
            'price_lighter': pl_float,
            'is_latency_arb': False,
            'expected_profit': expected_profit,
            'hours_to_breakeven': hours_to_breakeven
        })

    # Apply farm flag
    farm_mode_active = getattr(config, 'VOLUME_FARM_MODE', False)
    for opp in opps:
        if farm_mode_active:
            opp['is_farm_trade'] = True

    # Sort and deduplicate
    opps.sort(key=lambda x: x['apy'], reverse=True)
    
    unique_opps = {}
    for o in opps:
        sym = o['symbol']
        if sym not in unique_opps:
            unique_opps[sym] = o
        else:
            if o.get('is_latency_arb') and not unique_opps[sym].get('is_latency_arb'):
                unique_opps[sym] = o
    
    final_opps = list(unique_opps.values())
    final_opps.sort(key=lambda x: x['apy'], reverse=True)

    logger.info(f"✅ Found {len(final_opps)} opportunities from {valid_pairs} valid pairs")
    
    # Log rejection summary if no opportunities found (or periodically)
    if len(final_opps) == 0:
        logger.info(
            f"🚫 Filter Summary: "
            f"Open={rejected_open}, "
            f"Blacklist={rejected_blacklist}, "
            f"TradFi={rejected_tradfi}, "
            f"Cooldown={rejected_cooldown}, "
            f"Vol={rejected_volatility}, "
            f"Data={rejected_data}, "
            f"APY={rejected_apy}, "
            f"Spread={rejected_spread}, "
            f"Profit={rejected_profit}, "
            f"BE={rejected_breakeven}"
        )

    return final_opps[:config.MAX_OPEN_TRADES]
