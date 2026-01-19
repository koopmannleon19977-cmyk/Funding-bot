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
from decimal import Decimal
from typing import Any

import config
from src.application.fee_manager import get_fee_manager
from src.core.adaptive_threshold import get_threshold_manager
from src.core.latency_arb import get_detector, is_latency_arb_enabled
from src.core.orderbook_validator import simulate_price_impact
from src.utils import quantize_usd, safe_decimal

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
    notional_usd: Decimal,
    hourly_funding_rate: Decimal,
    hold_hours: Decimal,
    spread_pct: Decimal,
    x10_fee_rate: Decimal | None = None,
    lighter_fee_rate: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    """
    Calculate expected profit and hours to breakeven for a trade using Decimal.

    ⚡ KRITISCH: Verhindert Trades die nie profitabel werden!
    """
    # Get real fees from FeeManager if not provided
    if x10_fee_rate is None or lighter_fee_rate is None:
        try:
            fee_manager = get_fee_manager()
            if x10_fee_rate is None:
                x10_fee_rate = fee_manager.get_fees_for_exchange_decimal("X10", is_maker=False)
            if lighter_fee_rate is None:
                # Conservative: use taker fee for exit
                entry_fee_lit = fee_manager.get_fees_for_exchange_decimal("LIGHTER", is_maker=True)
                exit_fee_lit = fee_manager.get_fees_for_exchange_decimal("LIGHTER", is_maker=False)
            else:
                entry_fee_lit = lighter_fee_rate
                exit_fee_lit = lighter_fee_rate
        except Exception:
            x10_fee_rate = safe_decimal(getattr(config, "TAKER_FEE_X10", 0.000225))
            entry_fee_lit = safe_decimal(getattr(config, "MAKER_FEE_LIGHTER", 0.0))
            exit_fee_lit = safe_decimal(getattr(config, "TAKER_FEE_LIGHTER", 0.0))
    else:
        entry_fee_lit = lighter_fee_rate
        exit_fee_lit = lighter_fee_rate

    # Expected funding income over hold period
    funding_income = hourly_funding_rate * hold_hours * notional_usd

    # Total fees (X10 Taker Entry + Exit, Lighter Maker Entry + Taker Exit)
    total_fees = notional_usd * (x10_fee_rate * Decimal("2") + entry_fee_lit + exit_fee_lit)

    # Spread slippage cost
    slippage_cost = notional_usd * spread_pct

    # Total cost
    total_cost = total_fees + slippage_cost

    # Expected profit
    expected_profit = funding_income - total_cost

    # Hours to breakeven
    hourly_income = hourly_funding_rate * notional_usd
    if hourly_income > Decimal("0"):
        hours_to_breakeven = total_cost / hourly_income
    else:
        hours_to_breakeven = Decimal("999999")

    return quantize_usd(expected_profit), hours_to_breakeven


def _parse_best_price(level: Any) -> Decimal:
    """Parse a top-of-book level into a Decimal price."""
    try:
        if level is None:
            return Decimal("0")
        if isinstance(level, (list, tuple)) and len(level) > 0:
            return safe_decimal(level[0])
        if isinstance(level, dict):
            return safe_decimal(level.get("p") or level.get("price") or 0)
        return safe_decimal(level)
    except Exception:
        return Decimal("0")


def _best_bid_ask_from_orderbook(book: dict[str, Any]) -> tuple[Decimal, Decimal]:
    bids = (book or {}).get("bids") or []
    asks = (book or {}).get("asks") or []
    best_bid = _parse_best_price(bids[0]) if bids else Decimal("0")
    best_ask = _parse_best_price(asks[0]) if asks else Decimal("0")
    return best_bid, best_ask


def _derive_sides(leg1_exchange: str, leg1_side: str) -> tuple[str, str]:
    """Match src/core/trading.py side derivation to keep entry/EV consistent."""
    leg1_exchange = (leg1_exchange or "X10").strip()
    leg1_side = (leg1_side or "BUY").upper()
    x10_side = leg1_side if leg1_exchange == "X10" else ("SELL" if leg1_side == "BUY" else "BUY")
    lit_side = leg1_side if leg1_exchange == "Lighter" else ("SELL" if leg1_side == "BUY" else "BUY")
    return x10_side, lit_side


def _estimate_entry_prices(
    x10_bid: Decimal,
    x10_ask: Decimal,
    lit_bid: Decimal,
    lit_ask: Decimal,
    x10_side: str,
    lit_side: str,
) -> tuple[Decimal, Decimal]:
    """
    Entry pricing model using Decimal.
    - Lighter entry is Maker with PENNY JUMPING.
    - X10 entry is Taker hedge.
    """
    x10_side = (x10_side or "").upper()
    lit_side = (lit_side or "").upper()

    # X10 is always Taker: hits best bid/ask
    x10_entry = x10_ask if x10_side == "BUY" else x10_bid

    # Lighter is Maker with Penny Jumping
    lit_mid = (lit_bid + lit_ask) / Decimal("2") if (lit_bid > 0 and lit_ask > 0) else Decimal("0")
    price_tick = max(Decimal("0.0001"), lit_mid * Decimal("0.0005")) if lit_mid > 0 else Decimal("0.0001")

    spread = lit_ask - lit_bid
    min_safe_spread = price_tick * 2

    if lit_side == "SELL":
        if spread >= min_safe_spread:
            lit_entry = lit_ask - price_tick
            if lit_entry <= lit_bid + price_tick:
                lit_entry = lit_ask
        else:
            lit_entry = lit_ask
    else:  # BUY
        if spread >= min_safe_spread:
            lit_entry = lit_bid + price_tick
            if lit_entry >= lit_ask - price_tick:
                lit_entry = lit_bid
        else:
            lit_entry = lit_bid

    return x10_entry, lit_entry


def _estimate_exit_costs_usd(
    notional_usd: Decimal,
    exit_slippage_buffer_pct: Decimal,
    exit_cost_safety: Decimal,
) -> Decimal:
    exit_slip = notional_usd * exit_slippage_buffer_pct * exit_cost_safety
    return quantize_usd(exit_slip)


def _estimate_roundtrip_fees_usd(
    notional_usd: Decimal,
    x10_taker_fee: Decimal,
    lit_maker_fee: Decimal,
    lit_taker_fee: Decimal,
) -> Decimal:
    total = notional_usd * (x10_taker_fee * Decimal("2") + lit_maker_fee + lit_taker_fee)
    return quantize_usd(total)


def _estimate_price_pnl_to_basis_target_usd(
    notional_usd: Decimal,
    entry_price_x10: Decimal,
    entry_price_lighter: Decimal,
    x10_side: str,
    lit_side: str,
    basis_target: Decimal = Decimal("0"),
) -> tuple[Decimal, Decimal]:
    """
    Estimate price PnL using Decimal.
    Returns (basis_entry, expected_price_pnl_to_target_usd).
    """
    if entry_price_x10 <= 0 or entry_price_lighter <= 0:
        return Decimal("0"), Decimal("0")

    basis_entry = entry_price_lighter - entry_price_x10
    qty = notional_usd / max(entry_price_x10, entry_price_lighter)

    x10_side = (x10_side or "").upper()
    lit_side = (lit_side or "").upper()

    if x10_side == "BUY" and lit_side == "SELL":
        pnl = qty * (basis_entry - basis_target)
    elif x10_side == "SELL" and lit_side == "BUY":
        pnl = qty * (basis_target - basis_entry)
    else:
        pnl = Decimal("0")

    return basis_entry, quantize_usd(pnl)


# ============================================================
# MAIN OPPORTUNITY FINDER
# ============================================================
async def find_opportunities(lighter, x10, open_syms, is_farm_mode: bool = None) -> list[dict]:
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

    opps: list[dict] = []
    common = set(lighter.market_info.keys()) & set(x10.market_info.keys())
    threshold_manager = get_threshold_manager()
    detector = get_detector()  # ⚡ Latency Detector Instance

    # Verify market data is loaded
    if not common:
        logger.warning("⚠️ No common markets found")
        logger.debug(f"X10 markets: {len(x10.market_info)}, Lighter: {len(lighter.market_info)}")
        return []

    # Check price cache status - count actual valid prices (> 0) using sync cache access
    x10_prices = len([s for s in common if x10.fetch_mark_price_sync(s) > 0])
    lit_prices = len([s for s in common if lighter.fetch_mark_price_sync(s) > 0])
    logger.debug(f"Price cache status: X10={x10_prices}/{len(common)}, Lighter={lit_prices}/{len(common)}")

    # If many X10 prices are missing, trigger refresh
    if x10_prices < len(common) * 0.8:  # Less than 80% of prices available
        logger.info(f"⚠️ X10 price cache incomplete ({x10_prices}/{len(common)}) - triggering refresh")
        try:
            await x10.refresh_missing_prices()
        except Exception as e:
            logger.debug(f"X10 refresh_missing_prices error: {e}")

    if lit_prices == 0:
        logger.warning("⚠️ Lighter price cache completely empty - triggering refresh")
        await asyncio.gather(
            lighter.load_market_cache(force=True), lighter.load_funding_rates_and_prices(), return_exceptions=True
        )

    logger.debug(
        f"🔍 Scanning {len(common)} pairs. "
        f"Lighter markets: {len(lighter.market_info)}, X10 markets: {len(x10.market_info)}"
    )

    concurrency_limit = getattr(config, "OPP_SCAN_CONCURRENCY", 20)
    semaphore = asyncio.Semaphore(concurrency_limit)

    async def fetch_symbol_data(s: str):
        async with semaphore:
            try:
                # Get funding rates (from cache) - already Decimal from adapter
                lr = await lighter.fetch_funding_rate(s)
                xr = await x10.fetch_funding_rate(s)

                # Get prices (from cache) - already Decimal from adapter
                px = await x10.fetch_mark_price(s)
                pl = await lighter.fetch_mark_price(s)

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
                    symbol=s, x10_rate=rx, lighter_rate=rl, x10_adapter=x10, lighter_adapter=lighter
                )

                if latency_opp:
                    px_val = px or Decimal("0")
                    pl_val = pl or Decimal("0")
                    latency_opp["price_x10"] = px_val
                    latency_opp["price_lighter"] = pl_val
                    latency_opp["spread_pct"] = (abs(px_val - pl_val) / px_val) if px_val else Decimal("0")

                    logger.info(
                        f"⚡ LATENCY ARB DETECTED: {s} | "
                        f"Lag={latency_opp.get('lag_seconds', 0):.2f}s | "
                        f"Confidence={latency_opp.get('confidence', 0):.2f}"
                    )

                    latency_opportunities.append(latency_opp)

            except Exception as e:
                logger.debug(f"Latency check error for {s}: {e}")

        if latency_opportunities:
            latency_opportunities.sort(key=lambda x: x.get("confidence", 0) * x.get("lag_seconds", 0), reverse=True)
            logger.info(f"⚡ FAST LANE: {len(latency_opportunities)} Latency Arb opportunities!")
            return latency_opportunities[:1]

    # ═══════════════════════════════════════════════════════════════
    # STANDARD FUNDING ARBITRAGE
    # ═══════════════════════════════════════════════════════════════
    current_rates = [float(lr) for (_s, lr, _xr, _px, _pl) in clean_results if lr is not None]
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

    # Debug logging counter (limit to 5 entries)
    data_debug_logged = 0
    data_debug_limit = 5
    cache_status_logged = False  # Log cache status only once for first failed symbol

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

        # Validate data
        has_rates = rl is not None and rx is not None
        has_prices = px is not None and pl is not None and px > 0 and pl > 0

        if has_rates and has_prices:
            valid_pairs += 1
        else:
            rejected_data += 1
            # Debug logging...
            continue

        # Spread calculation (Decimal)
        spread = abs(px - pl) / px

        # Calculate funding metrics
        net = rl - rx
        apy = abs(net) * Decimal("24") * Decimal("365")

        req_apy = safe_decimal(threshold_manager.get_threshold(s, is_maker=True))
        if apy < req_apy:
            rejected_apy += 1
            continue

        # ═══════════════════════════════════════════════════════════════════════
        # DYNAMIC SPREAD FILTER (2025-12-22)
        # ═══════════════════════════════════════════════════════════════════════
        # Idee: Spread-Kosten sollten in angemessener Zeit durch Funding
        # zurückverdient werden können.
        #
        # Formel: max_spread = hourly_funding_rate × max_recovery_hours
        #
        # Beispiel:
        #   - Funding Rate = 0.01%/h (87.6% APY)
        #   - Max Recovery = 4 Stunden
        #   - Max Spread = 0.01% × 4 = 0.04% = 0.0004
        #
        # Bei höherer Funding Rate → höhere Spread-Toleranz!
        # Bei 60% APY (0.00685%/h) → Max Spread = 0.027% (mit 4h Recovery)
        # ═══════════════════════════════════════════════════════════════════════
        base_spread_limit = safe_decimal(config.MAX_SPREAD_FILTER_PERCENT)

        # Max hours to recover spread cost through funding (configurable)
        spread_recovery_hours = safe_decimal(getattr(config, "SPREAD_RECOVERY_HOURS", 4.0))

        # Dynamic limit: How much spread can we tolerate and still recover in X hours?
        hourly_rate = abs(net)  # This is hourly decimal rate
        funding_based_limit = hourly_rate * spread_recovery_hours

        # Use the HIGHER of base limit or funding-based limit (more permissive)
        # But cap at maximum 2% spread to avoid extreme cases
        max_spread_cap = safe_decimal(getattr(config, "MAX_SPREAD_CAP_PERCENT", 0.02))
        final_spread_limit = min(max(base_spread_limit, funding_based_limit), max_spread_cap)

        if spread > final_spread_limit:
            # Log why spread was rejected for debugging
            if spread > Decimal("0.001"):  # Only log significant spreads
                logger.debug(
                    f"🚫 {s}: Spread {float(spread) * 100:.3f}% > limit {float(final_spread_limit) * 100:.3f}% "
                    f"(funding={float(hourly_rate) * 100:.4f}%/h, recovery={float(spread_recovery_hours)}h)"
                )
            rejected_spread += 1
            continue

        # Profitability check (Decimal)
        notional = safe_decimal(getattr(config, "DESIRED_NOTIONAL_USD", 150.0))
        farm_mode = getattr(config, "VOLUME_FARM_MODE", False)

        min_hold_hours = safe_decimal(getattr(config, "MINIMUM_HOLD_SECONDS", 7200)) / Decimal("3600")

        if farm_mode:
            hold_hours = safe_decimal(getattr(config, "FARM_HOLD_SECONDS", 3600)) / Decimal("3600")
            max_breakeven_limit = hold_hours
        else:
            hold_hours = max(min_hold_hours, Decimal("24"))
            max_breakeven_limit = safe_decimal(getattr(config, "MAX_BREAKEVEN_HOURS", 8.0))

        entry_eval_hours = safe_decimal(getattr(config, "ENTRY_EVAL_HOURS", float(min_hold_hours)))
        entry_eval_hours = max(Decimal("0.25"), min(entry_eval_hours, hold_hours))

        min_profit_usd = safe_decimal(
            getattr(config, "MIN_EXPECTED_PROFIT_ENTRY_USD", getattr(config, "MIN_PROFIT_EXIT_USD", 0.10))
        )

        # Fee assumptions
        try:
            fee_manager = get_fee_manager()
            x10_fee_taker = fee_manager.get_fees_for_exchange_decimal("X10", is_maker=False)
            lit_fee_maker = fee_manager.get_fees_for_exchange_decimal("LIGHTER", is_maker=True)
            lit_fee_taker = fee_manager.get_fees_for_exchange_decimal("LIGHTER", is_maker=False)
        except Exception:
            x10_fee_taker = safe_decimal(getattr(config, "TAKER_FEE_X10", 0.000225))
            lit_fee_maker = safe_decimal(getattr(config, "MAKER_FEE_LIGHTER", 0.0))
            lit_fee_taker = safe_decimal(getattr(config, "TAKER_FEE_LIGHTER", 0.0))

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
            x10_bid = x10_ask = lit_bid = lit_ask = Decimal("0")

        entry_px_x10, entry_px_lit = _estimate_entry_prices(
            x10_bid=x10_bid,
            x10_ask=x10_ask,
            lit_bid=lit_bid,
            lit_ask=lit_ask,
            x10_side=x10_side,
            lit_side=lit_side,
        )

        basis_target = safe_decimal(getattr(config, "BASIS_EXIT_TARGET_USD", 0.0))
        basis_entry, expected_price_pnl_to_target = _estimate_price_pnl_to_basis_target_usd(
            notional_usd=notional,
            entry_price_x10=entry_px_x10 if entry_px_x10 > 0 else px,
            entry_price_lighter=entry_px_lit if entry_px_lit > 0 else pl,
            x10_side=x10_side,
            lit_side=lit_side,
            basis_target=basis_target,
        )

        require_favorable_basis = getattr(config, "REQUIRE_FAVORABLE_BASIS_ENTRY", True)
        basis_ok = (expected_price_pnl_to_target > 0) if require_favorable_basis else True

        roundtrip_fees = _estimate_roundtrip_fees_usd(
            notional_usd=notional,
            x10_taker_fee=x10_fee_taker,
            lit_maker_fee=lit_fee_maker,
            lit_taker_fee=lit_fee_taker,
        )
        exit_slip_pct = safe_decimal(getattr(config, "EXIT_SLIPPAGE_BUFFER_PCT", 0.0015))
        exit_safety = safe_decimal(getattr(config, "EXIT_COST_SAFETY_MARGIN", 1.1))
        exit_slippage_cost = _estimate_exit_costs_usd(
            notional_usd=notional,
            exit_slippage_buffer_pct=exit_slip_pct,
            exit_cost_safety=exit_safety,
        )

        entry_fees_usd = notional * (x10_fee_taker + lit_fee_maker)
        entry_edge_usd = expected_price_pnl_to_target - entry_fees_usd

        # ═══════════════════════════════════════════════════════════════════════
        # SMART PROFIT FILTER (2025-12-22)
        # ═══════════════════════════════════════════════════════════════════════
        # Bei Funding-Arbitrage ist negative Entry-Basis NICHT automatisch schlecht!
        # Wenn das Funding hoch genug ist, um negative Basis + Fees auszugleichen,
        # kann der Trade profitabel sein.
        #
        # NEU: Berechne wie schnell wir negative Entry-Kosten durch Funding
        # zurückverdienen können. Wenn < ENTRY_MAX_RECOVERY_HOURS → Trade OK!
        # ═══════════════════════════════════════════════════════════════════════
        entry_max_recovery_hours = safe_decimal(getattr(config, "ENTRY_MAX_RECOVERY_HOURS", 6.0))
        hourly_funding_income = abs(net) * notional

        if entry_edge_usd < 0:
            # Negative basis - check if funding can compensate
            if hourly_funding_income > 0:
                hours_to_recover_entry = abs(entry_edge_usd) / hourly_funding_income
                if hours_to_recover_entry > entry_max_recovery_hours:
                    # Too slow to recover - reject
                    logger.debug(
                        f"🚫 {s}: Negative entry edge ${float(entry_edge_usd):.2f}, "
                        f"recovery {float(hours_to_recover_entry):.1f}h > max {float(entry_max_recovery_hours)}h"
                    )
                    rejected_profit += 1
                    continue
                else:
                    # Funding can compensate - allow trade!
                    logger.debug(
                        f"✅ {s}: Negative entry ${float(entry_edge_usd):.2f} recoverable in "
                        f"{float(hours_to_recover_entry):.1f}h via funding"
                    )
            else:
                rejected_profit += 1
                continue

        hourly_rate = abs(net)
        funding_24h = hourly_rate * Decimal("24") * notional
        funding_eval = hourly_rate * entry_eval_hours * notional

        expected_profit_24h = quantize_usd(
            funding_24h + expected_price_pnl_to_target - roundtrip_fees - exit_slippage_cost
        )
        expected_profit_eval = quantize_usd(
            funding_eval + expected_price_pnl_to_target - roundtrip_fees - exit_slippage_cost
        )

        hourly_income = hourly_rate * notional
        remaining_cost = (roundtrip_fees + exit_slippage_cost) - expected_price_pnl_to_target
        if hourly_income > 0 and remaining_cost > 0:
            hours_to_breakeven = remaining_cost / hourly_income
        else:
            hours_to_breakeven = Decimal("0")

        if not farm_mode:
            if hours_to_breakeven > max_breakeven_limit:
                rejected_breakeven += 1
                continue
            if (not basis_ok) or (expected_profit_eval < min_profit_usd):
                rejected_profit += 1
                continue
        else:
            if hours_to_breakeven > max_breakeven_limit:
                rejected_breakeven += 1
                continue

        # Price impact simulation
        estimated_slippage_pct = float(spread * 100)
        max_slippage_pct = getattr(config, "MAX_PRICE_IMPACT_PCT", 0.5)

        try:
            if hasattr(lighter, "fetch_orderbook"):
                book = await lighter.fetch_orderbook(s, limit=20)
                if book and "bids" in book and "asks" in book:
                    price_impact_result = simulate_price_impact(
                        side=leg1_side,
                        order_size_usd=float(notional),
                        bids=book["bids"],
                        asks=book["asks"],
                        mid_price=float((pl + px) / 2),
                    )

                    if price_impact_result.can_fill:
                        estimated_slippage_pct = float(price_impact_result.slippage_percent)
                        if estimated_slippage_pct > max_slippage_pct:
                            continue
                    else:
                        continue
        except Exception:
            pass

        # ✅ Trade is profitable!
        # Calculate Pure Funding APY and Total APY
        # Note: We do NOT annualize one-time spread/fees by 365, as this leads to 1000%+ APYs.
        # Instead, we show Funding APY + One-time ROI.
        funding_apy_decimal = abs(net) * Decimal("24") * Decimal("365")
        one_time_roi = (expected_price_pnl_to_target - roundtrip_fees - exit_slippage_cost) / notional
        total_apy = funding_apy_decimal + one_time_roi

        logger.info(
            f"✅ {s}: [Rate Check] Lighter={float(rl) * 100:.6f}%/h, X10={float(rx) * 100:.6f}%/h | "
            f"Funding APY: {float(funding_apy_decimal) * 100:.1f}%, "
            f"Spread/Fees: {float(one_time_roi) * 100:.2f}%, "
            f"Total APY: {float(total_apy) * 100:.1f}% "
            f"(Exp. Profit: ${float(expected_profit_24h):.2f} in 24h, BE: {float(hours_to_breakeven):.2f}h)"
        )

        opps.append(
            {
                "symbol": s,
                "apy": float(total_apy * 100),  # Use Total APY for sorting
                "funding_apy": float(funding_apy_decimal * 100),
                "one_time_roi": float(one_time_roi * 100),
                "net_funding_hourly": float(net),
                "leg1_exchange": leg1_exchange,
                "leg1_side": leg1_side,
                "is_farm_trade": is_farm_mode,
                "spread_pct": float(spread),
                "price_x10": float(px),
                "price_lighter": float(pl),
                "is_latency_arb": False,
                "expected_profit": float(expected_profit_24h),
                "expected_profit_eval": float(expected_profit_eval),
                "hours_to_breakeven": float(hours_to_breakeven),
                "estimated_slippage_pct": estimated_slippage_pct,
                "entry_price_x10_est": float(entry_px_x10 if entry_px_x10 > 0 else px),
                "entry_price_lighter_est": float(entry_px_lit if entry_px_lit > 0 else pl),
                "basis_entry": float(basis_entry),
                "price_edge_to_basis_target": float(expected_price_pnl_to_target),
                "entry_edge_usd": float(entry_edge_usd),
                "roundtrip_fees_est": float(roundtrip_fees),
                "exit_slippage_cost_est": float(exit_slippage_cost),
            }
        )

    # Apply farm flag
    farm_mode_active = getattr(config, "VOLUME_FARM_MODE", False)
    for opp in opps:
        if farm_mode_active:
            opp["is_farm_trade"] = True

    # Sort and deduplicate
    opps.sort(key=lambda x: x["apy"], reverse=True)

    unique_opps = {}
    for o in opps:
        sym = o["symbol"]
        if sym not in unique_opps:
            unique_opps[sym] = o
        else:
            if o.get("is_latency_arb") and not unique_opps[sym].get("is_latency_arb"):
                unique_opps[sym] = o

    final_opps = list(unique_opps.values())
    final_opps.sort(key=lambda x: x["apy"], reverse=True)

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

    return final_opps[: config.MAX_OPEN_TRADES]
