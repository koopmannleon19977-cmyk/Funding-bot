"""
Scanning and validation logic for opportunities.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from funding_bot.domain.models import Opportunity, Side, Trade
from funding_bot.domain.rules import get_dynamic_max_spread
from funding_bot.observability.logging import get_logger
from funding_bot.services.liquidity_gates import (
    check_depth_for_entry_by_impact,
    check_l1_depth_for_entry,
    estimate_entry_spread_pct_by_impact,
    get_depth_gate_config,
)
from funding_bot.services.opportunities.evaluator import log_missing_historical_data_summary
from funding_bot.utils.decimals import safe_decimal as _safe_decimal

logger = get_logger(__name__)

# Track if historical data summary has been logged (once per session)
_historical_data_summary_logged = False


# =============================================================================
# COOLDOWN (Entry & Exit)
# =============================================================================


def mark_cooldown(self, symbol: str) -> None:
    """Mark a symbol as on cooldown after exit."""
    minutes = getattr(self.settings.trading, "cooldown_minutes", 60)
    self.cooldowns[symbol] = datetime.now(UTC) + timedelta(minutes=minutes)
    logger.info(f"Marked {symbol} for {minutes}m cooldown until {self.cooldowns[symbol]}")


def mark_rotation_cooldown(self, symbol: str) -> None:
    """Mark a symbol as on rotation cooldown after closing for rotation."""
    minutes = getattr(self.settings.trading, "opportunity_rotation_cooldown_minutes", 30)
    self.rotation_cooldowns[symbol] = datetime.now(UTC) + timedelta(minutes=minutes)
    logger.info(f"Marked {symbol} for {minutes}m rotation cooldown until {self.rotation_cooldowns[symbol]}")


async def scan(
    self,
    open_symbols: set[str],
    max_results: int = 10,
) -> list[Opportunity]:
    """
    Scan all markets for opportunities.

    Args:
        open_symbols: Symbols we already have trades for (excluded).
        max_results: Maximum number of opportunities to return.

    Returns:
        List of opportunities sorted by expected value (best first).
    """
    opportunities: list[Opportunity] = []
    symbols = self.market_data.get_common_symbols()

    logger.debug(f"Scanning {len(symbols)} symbols for opportunities...")

    # Fetch once per scan to keep candidate sizing consistent across symbols.
    available_equity = await self._get_available_equity_for_sizing()

    # 🚀 PERFORMANCE: Filter symbols first (sync), then evaluate in parallel (async)
    symbols_to_evaluate: list[str] = []
    now = datetime.now(UTC)

    for symbol in symbols:
        # Skip if we already have a trade
        if symbol in open_symbols:
            continue

        # === PHASE 4: CHECK BOTH COOLDOWNS ===
        # Check entry cooldown (after normal exit)
        if symbol in self.cooldowns:
            if now < self.cooldowns[symbol]:
                continue
            # Expired
            del self.cooldowns[symbol]

        # Check rotation cooldown (after rotation exit)
        if hasattr(self, "rotation_cooldowns") and symbol in self.rotation_cooldowns:
            if now < self.rotation_cooldowns[symbol]:
                continue
            # Expired
            del self.rotation_cooldowns[symbol]

        # === PHASE 4: CHECK SYMBOL FAILURE COOLDOWN ===
        # Skip symbols with too many consecutive failures (e.g., empty orderbooks)
        if hasattr(self, "should_skip_symbol") and self.should_skip_symbol(symbol):
            continue

        symbols_to_evaluate.append(symbol)

    # 🚀 PERFORMANCE: Parallel symbol evaluation with controlled concurrency
    # Premium-safe: batch_size=10 means ~10-20 API calls concurrently (well under 24k/min)
    batch_size = 10

    async def _evaluate_single(sym: str) -> Opportunity | None:
        """Evaluate a single symbol with error handling."""
        try:
            opp = await self._evaluate_symbol(sym, available_equity)
            if opp:
                # Record success to reset failure counter
                if hasattr(self, "record_symbol_success"):
                    self.record_symbol_success(sym)
                return opp
        except Exception as e:
            logger.debug(f"Failed to evaluate {sym}: {e}")
            # Record failure for retry tracking
            if hasattr(self, "record_symbol_failure"):
                self.record_symbol_failure(sym)
        return None

    # Process in batches with asyncio.gather for parallelism
    for i in range(0, len(symbols_to_evaluate), batch_size):
        batch = symbols_to_evaluate[i : i + batch_size]
        results = await asyncio.gather(*[_evaluate_single(sym) for sym in batch], return_exceptions=True)

        for result in results:
            if isinstance(result, Opportunity):
                opportunities.append(result)

    # Sort by expected value (descending)
    opportunities.sort(key=lambda o: o.expected_value_usd, reverse=True)

    # Limit results
    result = opportunities[:max_results]

    if result:
        top = result[0]
        if await self._reserve_scan_log(top):
            # Fetch real prices for the TOP symbol (Premium-friendly) so logs show actual Lighter/X10 prices.
            # Lighter has no batch price endpoint; we only do this for the best candidate.
            price_snapshot = self.market_data.get_price(top.symbol)
            if getattr(self.market_data.lighter, "_auth_token", None):
                try:
                    price_snapshot = await self.market_data.get_fresh_price(top.symbol)
                except Exception as e:
                    logger.debug(f"Failed to fetch fresh prices for {top.symbol}: {e}")

            lighter_px = price_snapshot.lighter_price if price_snapshot else Decimal("0")
            x10_px = price_snapshot.x10_price if price_snapshot else Decimal("0")
            spread = price_snapshot.spread_pct if price_snapshot else Decimal("0")
            logger.info(
                f"Found {len(opportunities)} opportunities, top: {top.symbol} "
                f"APY={top.apy:.1%} "
                f"(Lighter 1h: {top.lighter_rate:.4%}, X10 1h: {top.x10_rate:.4%}) "
                f"px(L/X)={lighter_px}/{x10_px} spread={spread:.3%} "
                f"EV=${top.expected_value_usd:.2f} "
                f"Size=${top.suggested_notional:.0f}"
            )
    else:
        # Throttled diagnostics so "0 opportunities" isn't silent.
        now = datetime.now(UTC)
        if self._last_zero_scan_log_at is None or (now - self._last_zero_scan_log_at).total_seconds() >= 60:
            self._last_zero_scan_log_at = now
            await self._log_zero_scan_diagnostics(
                open_symbols=open_symbols,
                available_equity=available_equity,
            )

    # Log summary of symbols with missing historical data (once per session)
    global _historical_data_summary_logged
    if not _historical_data_summary_logged:
        log_missing_historical_data_summary()
        _historical_data_summary_logged = True

    return result


async def get_best_opportunity(
    self,
    open_symbols: set[str],
    *,
    suppress_candidate_logs: bool = False,
) -> Opportunity | None:
    """
    Get the single best VALIDATED opportunity.

    This method performs a 'Lazy Fetch' validation:
    1. Gets top candidates based on cached data
    2. Fetches FRESH orderbook/price data for the top candidate
    3. Re-evaluates to ensure it still passes filters (especially spread)
    """
    # 1. Get top candidates from cached data
    candidates = await self.scan(open_symbols, max_results=3)
    if not candidates:
        return None

    # Use a single sizing baseline for all re-validations in this selection pass.
    available_equity = await self._get_available_equity_for_sizing()

    for opp in candidates:
        symbol = opp.symbol
        # 2. Fetch fresh data for this candidate only
        # This ensures we have real Liquidity and Bid/Ask spreads
        try:
            ob_snap = await self.market_data.get_fresh_orderbook(symbol)
            _ = await self.market_data.get_fresh_price(symbol)

            # Phase 4 Fix: Check if returned data has valid depth (not just partial/empty)
            # Graceful degradation returns partial data instead of raising, so we must check here
            has_lighter_depth = (ob_snap.lighter_bid_qty > 0 or ob_snap.lighter_ask_qty > 0) if ob_snap else False
            has_x10_depth = (ob_snap.x10_bid_qty > 0 or ob_snap.x10_ask_qty > 0) if ob_snap else False

            if not has_lighter_depth or not has_x10_depth:
                logger.debug(
                    f"Candidate {symbol} has partial orderbook data: "
                    f"lighter_depth={has_lighter_depth}, x10_depth={has_x10_depth}"
                )
                # Record as failure for skip logic
                if hasattr(self, "record_symbol_failure"):
                    self.record_symbol_failure(symbol)
                continue

        except Exception as e:
            logger.warning(f"Failed to fetch fresh data for candidate {symbol}: {e}")
            # Record failure for retry tracking (Phase 4)
            if hasattr(self, "record_symbol_failure"):
                self.record_symbol_failure(symbol)
            continue

        # 3. Re-evaluate with fresh data
        # _evaluate_symbol uses the cache, which we just updated
        fresh_opp, reject = await self._evaluate_symbol_with_reject_info(
            symbol,
            available_equity=available_equity,
            include_reject_info=True,
        )
        if not fresh_opp:
            # Keep the exact substring "rejected after fresh data fetch" for dashboard parsing.
            reason = reject.get("reason", "unknown")
            details = " ".join(f"{k}={v}" for k, v in reject.items() if k != "reason")
            suffix = f": {reason}" + (f" ({details})" if details else "")
            if await self._reserve_candidate_log(symbol, suppress=suppress_candidate_logs):
                logger.info(f"Candidate {symbol} rejected after fresh data fetch{suffix}")

            # Phase 4 Fix: Record depth-related failures for skip logic
            # Symbols that consistently fail depth checks should be temporarily skipped
            depth_failure = reason in ("depth_cap_failed", "depth_gate_failed", "no_orderbook")
            if depth_failure and hasattr(self, "record_symbol_failure"):
                self.record_symbol_failure(symbol)
            continue

        # 4. Explicit Strict Spread Check
        # This duplicates the check in ExecutionEngine to prevent "Executing..." logs for bad trades
        ts = self.settings.trading
        es = self.settings.execution

        # Direction logic
        long_exchange = "X10" if fresh_opp.lighter_rate > fresh_opp.x10_rate else "LIGHTER"

        # Get fresh orderbook
        ob = self.market_data.get_orderbook(symbol)
        if not ob:
            continue

        use_impact, depth_levels, depth_impact = get_depth_gate_config(ts)
        depth_book = None
        if getattr(ts, "depth_gate_enabled", False) and use_impact:
            try:
                depth_book = await self.market_data.get_fresh_orderbook_depth(
                    symbol,
                    levels=depth_levels,
                )
            except Exception as e:
                logger.debug(f"Depth fetch failed for {symbol} (validation): {e}")
                depth_book = None

        entry_spread = ob.entry_spread_pct(long_exchange)
        if getattr(es, "smart_pricing_enabled", False) and depth_book is not None:
            depth_spread, depth_metrics = estimate_entry_spread_pct_by_impact(
                depth_book,
                long_exchange=long_exchange,
                target_qty=fresh_opp.suggested_qty,
                max_price_impact_pct=depth_impact,
            )
            if depth_metrics.get("ok"):
                entry_spread = depth_spread
        max_spread = get_dynamic_max_spread(
            fresh_opp.apy,
            ts.max_spread_filter_percent,
            ts.max_spread_cap_percent,
        )

        guard_mult = _safe_decimal(
            getattr(ts, "negative_spread_guard_multiple", None),
            Decimal("2.0"),
        )
        guard_mult = max(Decimal("1.0"), min(guard_mult, Decimal("10.0")))
        guard_limit = max_spread * guard_mult
        if entry_spread < 0 and abs(entry_spread) > guard_limit:
            logger.debug(
                f"Candidate {symbol} favorable spread exceeds guard; "
                f"revalidating orderbook (spread={entry_spread:.4%}, guard={guard_limit:.4%})"
            )
            try:
                await self.market_data.get_fresh_orderbook(symbol)
                ob = self.market_data.get_orderbook(symbol) or ob
                if ob:
                    entry_spread = ob.entry_spread_pct(long_exchange)
            except Exception as e:
                logger.debug(f"Spread guard revalidation failed for {symbol}: {e}")

        spread_cost = entry_spread if entry_spread > 0 else Decimal("0")
        if spread_cost > max_spread:
            if await self._reserve_candidate_log(symbol, suppress=suppress_candidate_logs):
                logger.info(
                    f"Candidate {symbol} validated REJECTED: SpreadCost {spread_cost:.4%} > "
                    f"Max {max_spread:.4%} (raw={entry_spread:.4%})"
                )
            continue

        # 5. Liquidity/Depth gate (L1) on fresh orderbook, using suggested sizing.
        if getattr(ts, "depth_gate_enabled", False):
            lighter_side = Side.BUY if long_exchange == "LIGHTER" else Side.SELL
            if use_impact:
                try:
                    if depth_book is None:
                        depth_book = await self.market_data.get_fresh_orderbook_depth(
                            symbol,
                            levels=depth_levels,
                        )
                    depth_result = check_depth_for_entry_by_impact(
                        depth_book,
                        lighter_side=lighter_side,
                        x10_side=lighter_side.inverse(),
                        target_qty=fresh_opp.suggested_qty,
                        target_notional_usd=fresh_opp.suggested_notional,
                        min_l1_notional_usd=getattr(ts, "min_l1_notional_usd", Decimal("0")),
                        min_l1_notional_multiple=getattr(ts, "min_l1_notional_multiple", Decimal("0")),
                        max_l1_qty_utilization=getattr(ts, "max_l1_qty_utilization", Decimal("1.0")),
                        max_price_impact_pct=depth_impact,
                    )
                except Exception as e:
                    logger.debug(f"Depth gate fallback to L1 for {symbol}: {e}")
                    depth_result = check_l1_depth_for_entry(
                        ob,
                        lighter_side=lighter_side,
                        x10_side=lighter_side.inverse(),
                        target_qty=fresh_opp.suggested_qty,
                        target_notional_usd=fresh_opp.suggested_notional,
                        min_l1_notional_usd=getattr(ts, "min_l1_notional_usd", Decimal("0")),
                        min_l1_notional_multiple=getattr(ts, "min_l1_notional_multiple", Decimal("0")),
                        max_l1_qty_utilization=getattr(ts, "max_l1_qty_utilization", Decimal("1.0")),
                    )
            else:
                depth_result = check_l1_depth_for_entry(
                    ob,
                    lighter_side=lighter_side,
                    x10_side=lighter_side.inverse(),
                    target_qty=fresh_opp.suggested_qty,
                    target_notional_usd=fresh_opp.suggested_notional,
                    min_l1_notional_usd=getattr(ts, "min_l1_notional_usd", Decimal("0")),
                    min_l1_notional_multiple=getattr(ts, "min_l1_notional_multiple", Decimal("0")),
                    max_l1_qty_utilization=getattr(ts, "max_l1_qty_utilization", Decimal("1.0")),
                )
            if not depth_result.passed:
                if await self._reserve_candidate_log(symbol, suppress=suppress_candidate_logs):
                    logger.info(f"Candidate {symbol} validated REJECTED: insufficient depth ({depth_result.reason})")
                # Phase 4 Fix: Record depth failures for skip logic
                # Low-volume symbols consistently fail depth checks, so track them
                if hasattr(self, "record_symbol_failure"):
                    self.record_symbol_failure(symbol)
                continue

        # Passed all checks
        if await self._reserve_candidate_log(symbol, suppress=suppress_candidate_logs):
            logger.info(
                f"Candidate {symbol} VALIDATED. "
                f"Spread={entry_spread:.4%} Cost={spread_cost:.4%} "
                f"(Max {max_spread:.4%}). EV=${fresh_opp.expected_value_usd:.2f}"
            )
        return fresh_opp

    return None


# =============================================================================
# PHASE 4: ROTATION LOGIC (Switching Cost + NetEV Comparison)
# =============================================================================


async def calculate_switching_cost(
    self,
    current_trade: Trade,
    new_opp: Opportunity,
) -> dict:
    """
    Calculate the total cost to rotate from current position to new opportunity.

    Includes:
    - Exit cost of current position (weighted exit fees)
    - Entry cost of new position (fees + spread)
    - Latency penalty (configured USD amount)
    - Slippage buffer (optional, minimal for rotation)

    Args:
        current_trade: Currently open trade
        new_opp: New opportunity to rotate into

    Returns:
        Dict with total_switching_cost and breakdown
    """
    ts = self.settings.trading

    # Get fee schedule
    fees = self.market_data.get_fee_schedule(current_trade.symbol)

    # === 1. Exit cost of current position (weighted) ===
    # Import helper function to get exchange-specific probability
    from funding_bot.services.opportunities.scoring import _get_maker_fill_probability

    # Get exchange-specific maker fill probability (Lighter Premium assumed)
    # TODO: Detect actual account type from adapter/position
    p_maker = _get_maker_fill_probability(ts, exchange="lighter", account_type="premium")
    p_ioc = Decimal("1") - p_maker

    # Current position notional (use current mark price if available)
    price_data = self.market_data.get_price(current_trade.symbol)
    current_price = price_data.mid_price if price_data else Decimal("0")
    if current_price == 0:
        current_price = (current_trade.leg1.entry_price + current_trade.leg2.entry_price) / Decimal("2")

    current_notional = current_trade.leg1.filled_qty * current_price

    # Weighted exit fees for current position
    lighter_exit = current_notional * (
        p_maker * fees.get("lighter_maker", Decimal("0.00002")) + p_ioc * fees.get("lighter_taker", Decimal("0.0002"))
    )
    x10_exit = current_notional * (
        p_maker * fees.get("x10_maker", Decimal("0")) + p_ioc * fees.get("x10_taker", Decimal("0.000225"))
    )
    exit_cost = lighter_exit + x10_exit

    # === 2. Entry cost of new position ===
    # Entry fees for new position (same structure as current)
    entry_fees = new_opp.suggested_notional * fees.get(
        "lighter_maker", Decimal("0.00002")
    ) + new_opp.suggested_notional * fees.get("x10_taker", Decimal("0.000225"))

    # Entry spread cost (from opportunity's spread_pct)
    entry_spread_cost = new_opp.suggested_notional * new_opp.spread_pct

    # === 3. Latency penalty ===
    latency_penalty = getattr(ts, "switching_cost_latency_usd", Decimal("0.20"))

    # === 4. Total switching cost ===
    total_switching_cost = exit_cost + entry_fees + entry_spread_cost + latency_penalty

    return {
        "total_switching_cost": total_switching_cost,
        "exit_cost": exit_cost,
        "entry_fees": entry_fees,
        "entry_spread_cost": entry_spread_cost,
        "latency_penalty": latency_penalty,
    }


async def should_rotate_to(
    self,
    current_trade: Trade,
    new_opp: Opportunity,
    *,
    margin: Decimal = Decimal("0.05"),  # 5% improvement required
) -> tuple[bool, str]:
    """
    Determine if we should rotate from current position to new opportunity.

    Rotation logic:
    - NetEV_new_after_costs >= NetEV_current_after_costs * (1 + margin)
    - Both cooldowns must be respected (entry and rotation)

    Args:
        current_trade: Currently open trade
        new_opp: New opportunity to evaluate
        margin: Minimum improvement margin (default 5%)

    Returns:
        Tuple of (should_rotate: bool, reason: str)
    """
    ts = self.settings.trading

    # Check rotation cooldown first (cheaper check)
    rotation_cooldown_minutes = getattr(ts, "opportunity_rotation_cooldown_minutes", 30)
    if hasattr(self, "rotation_cooldowns") and current_trade.symbol in self.rotation_cooldowns:
        if datetime.now(UTC) < self.rotation_cooldowns[current_trade.symbol]:
            return False, f"Symbol in rotation cooldown for {rotation_cooldown_minutes}m"
        # Expired
        del self.rotation_cooldowns[current_trade.symbol]

    # Calculate NetEV for current position (remaining value)
    # NetEV_current = (current_funding_remaining) - (exit_cost_current)
    # For simplicity, use current_apy as proxy for remaining value
    current_funding_hourly = (
        current_trade.current_funding_hourly if hasattr(current_trade, "current_funding_hourly") else Decimal("0")
    )

    # Estimate remaining hold time (up to max_hold_hours)
    remaining_hold_hours = min(
        Decimal("24"),  # Assume 24h remaining for conservative estimate
        getattr(ts, "max_hold_hours", Decimal("240")) - Decimal(current_trade.hold_duration_seconds) / Decimal("3600"),
    )
    remaining_hold_hours = max(Decimal("1"), remaining_hold_hours)

    current_funding_remaining = abs(current_funding_hourly) * remaining_hold_hours

    # Calculate exit cost for current position
    switching = await self.calculate_switching_cost(current_trade, new_opp)
    current_exit_cost = switching["exit_cost"]

    # NetEV_current_after_exit = funding_remaining - exit_cost
    net_ev_current = current_funding_remaining - current_exit_cost

    # Calculate NetEV for new position
    # NetEV_new = (total_funding_new) - (entry_cost_new + exit_cost_new)
    # Use new_opp.expected_value_usd which already accounts for entry + exit costs
    net_ev_new = new_opp.expected_value_usd

    # Add switching cost to new position (we pay exit + entry + latency)
    net_ev_new_after_switching = net_ev_new - switching["total_switching_cost"]

    # === ROTATION DECISION ===
    # Rotate only if: NetEV_new_after_switching >= NetEV_current_after_exit * (1 + margin)
    required_ev = net_ev_current * (Decimal("1") + margin)

    if net_ev_new_after_switching >= required_ev:
        improvement = (net_ev_new_after_switching - net_ev_current) / max(net_ev_current, Decimal("0.01"))
        return True, (
            f"Rotation profitable: New NetEV=${net_ev_new_after_switching:.2f} >= "
            f"Required ${required_ev:.2f} (Current ${net_ev_current:.2f} + {margin:.0%} margin). "
            f"Improvement: {improvement:.1%}"
        )
    else:
        shortfall = required_ev - net_ev_new_after_switching
        return False, (
            f"Rotation not profitable: New NetEV=${net_ev_new_after_switching:.2f} < "
            f"Required ${required_ev:.2f}. Shortfall: ${shortfall:.2f}"
        )
