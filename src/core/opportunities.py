# src/core/opportunities.py
"""
Opportunity detection for funding arbitrage.

This module handles:
- Finding trading opportunities across exchanges
- Latency arbitrage detection
- Profitability calculations
- Filtering (blacklist, volatility, spread limits)
- Price impact simulation (H7)

FIXED (2025-12-17): Uses FeeManager for real exchange fees instead of config defaults.
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
from src.validation.orderbook_validator import simulate_price_impact, PriceImpactResult
from src.fee_manager import get_fee_manager

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
    x10_fee_rate: float = None,
    lighter_fee_rate: float = None
) -> tuple:
    """
    Calculate expected profit and hours to breakeven for a trade.
    
    ⚡ KRITISCH: Verhindert Trades die nie profitabel werden!
    
    FIXED (2025-12-17): Now uses FeeManager for real exchange fees.
    
    Args:
        notional_usd: Trade size in USD
        hourly_funding_rate: Net funding rate per hour (|rx - rl|)
        hold_hours: Expected hold duration in hours
        spread_pct: Current spread as decimal
        x10_fee_rate: X10 fee rate (default from FeeManager)
        lighter_fee_rate: Lighter fee rate (default from FeeManager)
        
    Returns:
        (expected_profit_usd: float, hours_to_breakeven: float)
    """
    # Get real fees from FeeManager if not provided
    if x10_fee_rate is None or lighter_fee_rate is None:
        try:
            fee_manager = get_fee_manager()
            if x10_fee_rate is None:
                # Conservative: assume Taker for both entry and exit on X10
                x10_fee_rate = float(fee_manager.get_fees_for_exchange_decimal('X10', is_maker=False))
            if lighter_fee_rate is None:
                # Lighter: Maker entry, Taker exit (conservative)
                entry_fee_lit = float(fee_manager.get_fees_for_exchange_decimal('LIGHTER', is_maker=True))
                exit_fee_lit = float(fee_manager.get_fees_for_exchange_decimal('LIGHTER', is_maker=False))
        except Exception:
            # Fallback to config if FeeManager not available
            x10_fee_rate = getattr(config, 'TAKER_FEE_X10', 0.000225)
            entry_fee_lit = getattr(config, 'MAKER_FEE_LIGHTER', 0.0)
            exit_fee_lit = getattr(config, 'TAKER_FEE_LIGHTER', 0.0)
    else:
        entry_fee_lit = lighter_fee_rate
        exit_fee_lit = lighter_fee_rate
        
    # Convert all inputs to Decimal for precision
    notional = safe_decimal(notional_usd)
    rate = safe_decimal(abs(hourly_funding_rate))
    hours = safe_decimal(hold_hours)
    spread = safe_decimal(spread_pct)
    
    # Expected funding income over hold period
    funding_income = rate * hours * notional
    
    # Entry + Exit fees on both exchanges
    # X10: Entry (Taker/Maker) + Exit (Taker)
    # Lighter: Entry (Maker) + Exit (Taker)
    fee_x10 = safe_decimal(x10_fee_rate) 
    fee_lit_entry = safe_decimal(entry_fee_lit)
    fee_lit_exit = safe_decimal(exit_fee_lit)
    
    # We assume X10 Taker entry + Taker exit for conservatism
    total_fees = notional * (fee_x10 * Decimal('2') + fee_lit_entry + fee_lit_exit)
    
    # Spread slippage cost (estimated as full spread across both legs)
    # If mid-price is used, we pay half spread on each exchange.
    slippage_cost = notional * spread
    
    # Total cost
    total_cost = total_fees + slippage_cost
    
    # Expected profit
    expected_profit = funding_income - total_cost
    
    # Hours to breakeven
    hourly_income = rate * notional
    if hourly_income > Decimal('0'):
        hours_to_breakeven = total_cost / hourly_income
    else:
        hours_to_breakeven = Decimal('999999')
    
    return float(quantize_usd(expected_profit)), float(hours_to_breakeven)


def _parse_best_price(level: Any) -> float:
    """Parse a top-of-book level into a float price."""
    try:
        if level is None:
            return 0.0
        if isinstance(level, (list, tuple)) and len(level) > 0:
            return safe_float(level[0], 0.0)
        if isinstance(level, dict):
            return safe_float(level.get("p") or level.get("price") or 0.0, 0.0)
        return safe_float(level, 0.0)
    except Exception:
        return 0.0


def _best_bid_ask_from_orderbook(book: Dict[str, Any]) -> tuple[float, float]:
    bids = (book or {}).get("bids") or []
    asks = (book or {}).get("asks") or []
    best_bid = _parse_best_price(bids[0]) if bids else 0.0
    best_ask = _parse_best_price(asks[0]) if asks else 0.0
    return best_bid, best_ask


def _derive_sides(leg1_exchange: str, leg1_side: str) -> tuple[str, str]:
    """Match src/core/trading.py side derivation to keep entry/EV consistent."""
    leg1_exchange = (leg1_exchange or "X10").strip()
    leg1_side = (leg1_side or "BUY").upper()
    x10_side = leg1_side if leg1_exchange == "X10" else ("SELL" if leg1_side == "BUY" else "BUY")
    lit_side = leg1_side if leg1_exchange == "Lighter" else ("SELL" if leg1_side == "BUY" else "BUY")
    return x10_side, lit_side


def _estimate_entry_prices(
    x10_bid: float,
    x10_ask: float,
    lit_bid: float,
    lit_ask: float,
    x10_side: str,
    lit_side: str,
) -> tuple[float, float]:
    """
    Entry pricing model aligned with current execution:
    - Lighter entry is Maker: SELL hits best ask, BUY hits best bid
    - X10 entry is Taker hedge: BUY hits best ask, SELL hits best bid
    """
    x10_side = (x10_side or "").upper()
    lit_side = (lit_side or "").upper()
    x10_entry = x10_ask if x10_side == "BUY" else x10_bid
    lit_entry = lit_ask if lit_side == "SELL" else lit_bid
    return safe_float(x10_entry, 0.0), safe_float(lit_entry, 0.0)


def _estimate_exit_costs_usd(
    notional_usd: float,
    exit_slippage_buffer_pct: float,
    exit_cost_safety: float,
) -> float:
    notional = safe_decimal(notional_usd)
    slip = safe_decimal(exit_slippage_buffer_pct)
    safety = safe_decimal(exit_cost_safety)
    exit_slip = notional * slip * safety
    return float(quantize_usd(exit_slip))


def _estimate_roundtrip_fees_usd(
    notional_usd: float,
    x10_taker_fee: float,
    lit_maker_fee: float,
    lit_taker_fee: float,
) -> float:
    notional = safe_decimal(notional_usd)
    fee_x10 = safe_decimal(x10_taker_fee)
    fee_lit_m = safe_decimal(lit_maker_fee)
    fee_lit_t = safe_decimal(lit_taker_fee)
    total = notional * (fee_x10 * Decimal("2") + fee_lit_m + fee_lit_t)
    return float(quantize_usd(total))


def _estimate_price_pnl_to_basis_target_usd(
    notional_usd: float,
    entry_price_x10: float,
    entry_price_lighter: float,
    x10_side: str,
    lit_side: str,
    basis_target: float = 0.0,
) -> tuple[float, float]:
    """
    Estimate the price PnL if the cross-exchange basis closes to a target.
    Returns (basis_entry, expected_price_pnl_to_target_usd).
    """
    px = safe_float(entry_price_x10, 0.0)
    pl = safe_float(entry_price_lighter, 0.0)
    if px <= 0 or pl <= 0:
        return 0.0, 0.0

    basis_entry = pl - px
    qty = safe_decimal(notional_usd) / safe_decimal(max(px, pl))
    qty_f = float(qty) if qty > 0 else 0.0

    x10_side = (x10_side or "").upper()
    lit_side = (lit_side or "").upper()

    # Hedge shapes:
    # - BUY X10 / SELL Lighter profits when basis decreases (pl - px falls)
    # - SELL X10 / BUY Lighter profits when basis increases
    if x10_side == "BUY" and lit_side == "SELL":
        pnl = qty_f * (basis_entry - basis_target)
    elif x10_side == "SELL" and lit_side == "BUY":
        pnl = qty_f * (basis_target - basis_entry)
    else:
        pnl = 0.0

    return basis_entry, float(quantize_usd(safe_decimal(pnl)))


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

        # ═══════════════════════════════════════════════════════════════
        # H8: DYNAMIC SPREAD CHECK - Volatility-adjusted spread threshold
        # Low vol = stricter, High vol = relaxed (but still capped)
        # ═══════════════════════════════════════════════════════════════
        vol_monitor = get_volatility_monitor()
        base_spread_limit = config.MAX_SPREAD_FILTER_PERCENT
        
        # H8: Get volatility-adjusted spread limit
        dynamic_spread_limit = vol_monitor.get_dynamic_spread_limit(s, base_spread_limit)
        
        # Also consider funding boost (high funding can justify wider spread)
        funding_boosted_limit = abs(net) * 12.0
        final_spread_limit = min(max(dynamic_spread_limit, funding_boosted_limit), 0.03)
        
        if spread > final_spread_limit:
            # logger.debug(f"🚫 {s}: Spread {spread*100:.2f}% > Limit {final_spread_limit*100:.2f}%")
            rejected_spread += 1
            continue

        # Profitability check
        notional = getattr(config, 'DESIRED_NOTIONAL_USD', 150.0)
        farm_mode = getattr(config, 'VOLUME_FARM_MODE', False)
        
        # Consistent hold hours for profit gate
        min_hold_hours = getattr(config, 'MINIMUM_HOLD_SECONDS', 7200) / 3600
        
        if farm_mode:
            hold_hours = getattr(config, 'FARM_HOLD_SECONDS', 3600) / 3600
            max_breakeven_limit = hold_hours
        else:
            # For standard arb, we use a conservative hold window for the entry gate
            # but allow the breakeven to be up to MAX_BREAKEVEN_HOURS
            hold_hours = max(min_hold_hours, 24.0) # We still use 24h for "expected" but check BE strictly
            max_breakeven_limit = getattr(config, 'MAX_BREAKEVEN_HOURS', 8.0)

        # Entry EV gate: evaluate at the minimum realistic hold window.
        min_hold_hours = getattr(config, 'MINIMUM_HOLD_SECONDS', 7200) / 3600
        entry_eval_hours = float(getattr(config, "ENTRY_EVAL_HOURS", min_hold_hours))
        entry_eval_hours = max(0.25, min(entry_eval_hours, hold_hours))

        min_profit_usd = float(getattr(config, "MIN_EXPECTED_PROFIT_ENTRY_USD", getattr(config, 'MIN_PROFIT_EXIT_USD', 0.10)))

        # Fee assumptions (execution-aligned)
        try:
            fee_manager = get_fee_manager()
            x10_fee_taker = float(fee_manager.get_fees_for_exchange_decimal("X10", is_maker=False))
            lit_fee_maker = float(fee_manager.get_fees_for_exchange_decimal("LIGHTER", is_maker=True))
            lit_fee_taker = float(fee_manager.get_fees_for_exchange_decimal("LIGHTER", is_maker=False))
        except Exception:
            x10_fee_taker = float(getattr(config, "TAKER_FEE_X10", 0.000225))
            lit_fee_maker = float(getattr(config, "MAKER_FEE_LIGHTER", 0.0))
            lit_fee_taker = float(getattr(config, "TAKER_FEE_LIGHTER", 0.0))

        # Orderbook-based entry basis (directed) to avoid "ignore spread" mistakes.
        leg1_exchange = "Lighter" if rl > rx else "X10"
        leg1_side = "SELL" if rl > rx else "BUY"
        x10_side, lit_side = _derive_sides(leg1_exchange, leg1_side)

        try:
            x10_book, lit_book = await asyncio.gather(
                x10.fetch_orderbook(s, limit=1),
                lighter.fetch_orderbook(s, limit=1),
                return_exceptions=True,
            )
            x10_book = {"bids": [], "asks": []} if isinstance(x10_book, Exception) else (x10_book or {})
            lit_book = {"bids": [], "asks": []} if isinstance(lit_book, Exception) else (lit_book or {})
            x10_bid, x10_ask = _best_bid_ask_from_orderbook(x10_book)
            lit_bid, lit_ask = _best_bid_ask_from_orderbook(lit_book)
        except Exception:
            x10_bid = x10_ask = lit_bid = lit_ask = 0.0

        entry_px_x10, entry_px_lit = _estimate_entry_prices(
            x10_bid=x10_bid,
            x10_ask=x10_ask,
            lit_bid=lit_bid,
            lit_ask=lit_ask,
            x10_side=x10_side,
            lit_side=lit_side,
        )

        basis_target = float(getattr(config, "BASIS_EXIT_TARGET_USD", 0.0))
        basis_entry, expected_price_pnl_to_target = _estimate_price_pnl_to_basis_target_usd(
            notional_usd=notional,
            entry_price_x10=entry_px_x10 or px_float,
            entry_price_lighter=entry_px_lit or pl_float,
            x10_side=x10_side,
            lit_side=lit_side,
            basis_target=basis_target,
        )

        # Basis direction check (Quantzilla: don't ignore price spread at entry.)
        # Default: require favorable basis for the hedge shape (immediate edge if basis closes).
        require_favorable_basis = getattr(config, "REQUIRE_FAVORABLE_BASIS_ENTRY", True)
        basis_ok = (expected_price_pnl_to_target > 0.0) if require_favorable_basis else True

        roundtrip_fees = _estimate_roundtrip_fees_usd(
            notional_usd=notional,
            x10_taker_fee=x10_fee_taker,
            lit_maker_fee=lit_fee_maker,
            lit_taker_fee=lit_fee_taker,
        )
        exit_slip_pct = float(getattr(config, "EXIT_SLIPPAGE_BUFFER_PCT", 0.0015))
        exit_safety = float(getattr(config, "EXIT_COST_SAFETY_MARGIN", 1.1))
        exit_slippage_cost = _estimate_exit_costs_usd(
            notional_usd=notional,
            exit_slippage_buffer_pct=exit_slip_pct,
            exit_cost_safety=exit_safety,
        )

        # Funding income (profit-positive) at horizons
        hourly_rate = abs(net)
        funding_24h = safe_float(hourly_rate * 24.0 * notional, 0.0)
        funding_eval = safe_float(hourly_rate * entry_eval_hours * notional, 0.0)

        expected_profit_24h = float(
            quantize_usd(
                safe_decimal(funding_24h + expected_price_pnl_to_target - roundtrip_fees - exit_slippage_cost)
            )
        )
        expected_profit_eval = float(
            quantize_usd(
                safe_decimal(funding_eval + expected_price_pnl_to_target - roundtrip_fees - exit_slippage_cost)
            )
        )

        # Breakeven hours: how long funding needs to cover (fees+exit_costs - expected price edge)
        hourly_income = safe_decimal(abs(hourly_rate)) * safe_decimal(notional)
        remaining_cost = safe_decimal(roundtrip_fees + exit_slippage_cost) - safe_decimal(expected_price_pnl_to_target)
        if hourly_income > 0 and remaining_cost > 0:
            hours_to_breakeven = float(remaining_cost / hourly_income)
        else:
            hours_to_breakeven = 0.0
        
        # Rejection logic
        if not farm_mode:
            if hours_to_breakeven > max_breakeven_limit:
                # logger.debug(f"🚫 {s}: Breakeven {hours_to_breakeven:.1f}h > Limit {max_breakeven_limit}h")
                rejected_breakeven += 1
                continue
                
            if (not basis_ok) or (expected_profit_eval < min_profit_usd):
                # Reject if entry basis is unfavorable or short-horizon EV doesn't clear minimum.
                rejected_profit += 1
                continue
        else:
            # Farm mode: just require breakeven within farm hold time
            if hours_to_breakeven > max_breakeven_limit:
                rejected_breakeven += 1
                continue
        
        # ═══════════════════════════════════════════════════════════════
        # NEU: LIQUIDITY CHECK - Genug Orderbook-Tiefe für Entry?
        # Verhindert Trades in illiquiden Markets
        # ═══════════════════════════════════════════════════════════════
        min_depth = getattr(config, 'MIN_ORDERBOOK_DEPTH_USD', 100.0)
        try:
            # Check Lighter Liquidity (nur wenn Adapter verfügbar)
            if hasattr(lighter, 'check_liquidity'):
                lighter_liquid = await lighter.check_liquidity(
                    s, 'BUY' if rl > rx else 'SELL', 
                    notional, max_slippage_pct=0.01, is_maker=True
                )
                if not lighter_liquid:
                    logger.debug(f"🚫 {s}: Lighter Orderbook zu dünn für ${notional:.0f}")
                    continue
        except Exception as e:
            logger.debug(f"Liquidity check skipped for {s}: {e}")
        
        # ═══════════════════════════════════════════════════════════════
        # H7: PRICE IMPACT SIMULATION - Echte Slippage über Orderbook-Levels
        # Berechnet tatsächliche Ausführungskosten statt geschätztem Spread
        # ═══════════════════════════════════════════════════════════════
        estimated_slippage_pct = spread * 100  # Default: use spread as estimate
        price_impact_result = None
        
        max_slippage_pct = getattr(config, 'MAX_PRICE_IMPACT_PCT', 0.5)  # Default 0.5%
        
        try:
            # Get orderbook for price impact simulation
            if hasattr(lighter, 'fetch_orderbook'):
                book = await lighter.fetch_orderbook(s, limit=20)
                if book and 'bids' in book and 'asks' in book:
                    leg1_side = 'SELL' if rl > rx else 'BUY'
                    
                    price_impact_result = simulate_price_impact(
                        side=leg1_side,
                        order_size_usd=notional,
                        bids=book['bids'],
                        asks=book['asks'],
                        mid_price=(pl_float + px_float) / 2 if pl_float and px_float else None,
                    )
                    
                    if price_impact_result.can_fill:
                        estimated_slippage_pct = float(price_impact_result.slippage_percent)
                        
                        # Filter by max allowed slippage
                        if estimated_slippage_pct > max_slippage_pct:
                            logger.debug(
                                f"🚫 {s}: Price impact too high ({estimated_slippage_pct:.3f}% > {max_slippage_pct}%)"
                            )
                            continue
                        
                        logger.debug(
                            f"📊 {s}: Price impact simulation: "
                            f"slippage={estimated_slippage_pct:.4f}%, "
                            f"levels={price_impact_result.levels_consumed}, "
                            f"avg_price=${float(price_impact_result.avg_execution_price):.6f}"
                        )
                    else:
                        logger.debug(f"🚫 {s}: Cannot fill ${notional:.0f} in orderbook")
                        continue
                        
        except Exception as e:
            logger.debug(f"Price impact simulation skipped for {s}: {e}")
        
        # ✅ Trade is profitable!
        logger.info(
            f"✅ {s}: Expected profit ${expected_profit_24h:.4f} in 24.0h "
            f"(breakeven: {hours_to_breakeven:.2f}h, APY: {apy*100:.1f}%) | "
            f"Eval{entry_eval_hours:.2f}h=${expected_profit_eval:.4f}, "
            f"Basis=${basis_entry:.6f}, PriceEdge=${expected_price_pnl_to_target:.4f}, Fees=${roundtrip_fees:.4f}"
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
            'expected_profit': expected_profit_24h,
            'expected_profit_eval': expected_profit_eval,
            'hours_to_breakeven': hours_to_breakeven,
            'estimated_slippage_pct': estimated_slippage_pct,  # H7: Real slippage from simulation
            # Entry basis/edge diagnostics (execution-aligned)
            'entry_price_x10_est': entry_px_x10 or px_float,
            'entry_price_lighter_est': entry_px_lit or pl_float,
            'basis_entry': basis_entry,
            'price_edge_to_basis_target': expected_price_pnl_to_target,
            'roundtrip_fees_est': roundtrip_fees,
            'exit_slippage_cost_est': exit_slippage_cost,
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
