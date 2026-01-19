"""
Domain Rules: Entry and Exit conditions.

Pure functions that encode business logic for trading decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.models import (
    Balance,
    Opportunity,
    Side,
    Trade,
)
from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


# =============================================================================
# Reason Code Prefixes (Consistent Exit Reason Formatting)
# =============================================================================
REASON_EMERGENCY = "EMERGENCY"
REASON_REBALANCE = "REBALANCE"
REASON_EARLY_TP = "EARLY_TAKE_PROFIT"
REASON_EARLY_EDGE = "EARLY_EDGE"
REASON_NETEV = "NetEV"
REASON_YIELD_COST = "YIELD_COST"
REASON_BASIS = "BASIS"
REASON_VELOCITY = "VELOCITY"
REASON_Z_SCORE = "Z-SCORE"


@dataclass(frozen=True)
class RuleResult:
    """Result of a rule check."""

    passed: bool
    reason: str = ""


# =============================================================================
# Entry Rules
# =============================================================================


def check_min_apy(opportunity: Opportunity, min_apy: Decimal) -> RuleResult:
    """Check if opportunity meets minimum APY threshold."""
    if opportunity.apy >= min_apy:
        return RuleResult(True)
    return RuleResult(False, f"APY {opportunity.apy:.2%} < min {min_apy:.2%}")


def check_max_spread(opportunity: Opportunity, max_spread: Decimal) -> RuleResult:
    """Check if spread is within acceptable limits."""
    if opportunity.spread_pct <= max_spread:
        return RuleResult(True)
    return RuleResult(False, f"Spread {opportunity.spread_pct:.4%} > max {max_spread:.4%}")


def get_dynamic_max_spread(
    apy: Decimal,
    base_max_spread: Decimal,
    cap_max_spread: Decimal | None = None,
) -> Decimal:
    """
    Calculate dynamic max spread tolerance based on APY.

    UPDATED 2025-12-28: Tighter spreads for high APY!
    High APY often means illiquid assets with wide orderbook gaps.
    Allowing higher spreads for high APY was backwards logic.

    Evidence: MEGA (109% APY) consistently had 0.7-1.0% realized spread,
    causing immediate losses, while POPCAT (53% APY) had negative spreads.
    """
    # Note: These are MAXIMUM tolerances, not targets.
    # Lower is better - these are safety gates.
    if cap_max_spread is None:
        cap_max_spread = Decimal("1.0")  # Effectively no cap

    apy_cap = base_max_spread
    if apy > Decimal("1.00"):  # > 100% APY
        apy_cap = Decimal("0.005")  # 0.5%
    elif apy > Decimal("0.50"):  # > 50% APY
        apy_cap = Decimal("0.004")  # 0.4%
    elif apy > Decimal("0.20"):  # > 20% APY
        apy_cap = Decimal("0.003")  # 0.3%

    # Never loosen above the base filter; optionally apply an absolute cap.
    return min(base_max_spread, apy_cap, cap_max_spread)


def check_min_expected_profit(opportunity: Opportunity, min_profit_usd: Decimal) -> RuleResult:
    """Check if expected profit meets threshold."""
    if opportunity.expected_value_usd >= min_profit_usd:
        return RuleResult(True)
    return RuleResult(
        False,
        f"EV ${opportunity.expected_value_usd:.2f} < min ${min_profit_usd:.2f}",
    )


def check_max_breakeven(opportunity: Opportunity, max_hours: Decimal) -> RuleResult:
    """Check if breakeven time is acceptable."""
    if opportunity.breakeven_hours <= max_hours:
        return RuleResult(True)
    return RuleResult(
        False,
        f"Breakeven {opportunity.breakeven_hours:.1f}h > max {max_hours:.1f}h",
    )


def check_liquidity(opportunity: Opportunity, min_score: Decimal = Decimal("0.5")) -> RuleResult:
    """Check if liquidity score is acceptable."""
    if opportunity.liquidity_score >= min_score:
        return RuleResult(True)
    return RuleResult(
        False,
        f"Liquidity score {opportunity.liquidity_score:.2f} < min {min_score:.2f}",
    )


def check_balance(
    required_notional: Decimal,
    balance: Balance,
    margin_buffer: Decimal = Decimal("1.2"),
) -> RuleResult:
    """Check if balance is sufficient with buffer."""
    required = required_notional * margin_buffer
    if balance.available >= required:
        return RuleResult(True)
    return RuleResult(
        False,
        f"Balance ${balance.available:.2f} < required ${required:.2f}",
    )


def check_max_trades(current_count: int, max_trades: int) -> RuleResult:
    """Check if we can open another trade."""
    if current_count < max_trades:
        return RuleResult(True)
    return RuleResult(False, f"Max trades reached: {current_count}/{max_trades}")


def check_symbol_not_blacklisted(symbol: str, blacklist: set[str]) -> RuleResult:
    """Check if symbol is not blacklisted."""
    if symbol not in blacklist:
        return RuleResult(True)
    return RuleResult(False, f"Symbol {symbol} is blacklisted")


def check_not_already_in_trade(symbol: str, open_trade_symbols: set[str]) -> RuleResult:
    """Check if we don't already have a trade for this symbol."""
    if symbol not in open_trade_symbols:
        return RuleResult(True)
    return RuleResult(False, f"Already have trade for {symbol}")


# =============================================================================
# Exit Rules
# =============================================================================


def check_profit_target(trade: Trade, current_pnl: Decimal, target_usd: Decimal) -> RuleResult:
    """Check if profit target reached."""
    if current_pnl >= target_usd:
        return RuleResult(True, f"Profit target hit: ${current_pnl:.2f}")
    return RuleResult(False)


def check_min_hold_time(trade: Trade, min_seconds: int) -> RuleResult:
    """Check if minimum hold time has passed."""
    if trade.hold_duration_seconds >= min_seconds:
        return RuleResult(True)
    return RuleResult(
        False,
        f"Hold time {trade.hold_duration_seconds:.0f}s < min {min_seconds}s",
    )


def check_max_hold_time(
    trade: Trade,
    max_hours: Decimal,
    *,
    extend_on_recovery_prediction: bool = False,
    predicted_recovery_p50_minutes: int | None = None,
) -> RuleResult:
    """
    Check if maximum hold time exceeded.

    If extend_on_recovery_prediction is enabled and historical data predicts
    the current crash will recover soon, the max hold time can be extended
    to avoid premature exits during temporary downturns.
    """
    max_seconds = float(max_hours) * 3600
    current_seconds = trade.hold_duration_seconds

    # Base check: haven't reached max hold yet
    if current_seconds < max_seconds:
        return RuleResult(False)

    # We've reached max hold time - check if we should extend
    if extend_on_recovery_prediction and predicted_recovery_p50_minutes is not None:
        # Calculate remaining time until predicted recovery
        remaining_time_until_recovery = float(predicted_recovery_p50_minutes) * 60

        # If recovery is predicted within reasonable time (e.g., within 24 hours),
        # extend the hold to avoid exiting right before recovery
        # Only extend if recovery is imminent (within 6 hours)
        if remaining_time_until_recovery <= 21600:  # 6 hours in seconds
            return RuleResult(
                False,
                f"Max hold time reached ({max_hours}h) but extending: "
                f"recovery predicted in {predicted_recovery_p50_minutes}min",
            )

    return RuleResult(True, f"Max hold time reached: {max_hours}h")


def check_funding_flip(
    trade: Trade,
    current_lighter_rate: Decimal,
    current_x10_rate: Decimal,
    hours_threshold: Decimal,
    current_pnl: Decimal,
    estimated_exit_cost_usd: Decimal,
) -> RuleResult:
    """
    Check if funding has flipped against us and evaluate exit decision.

    NOTE: This function is only called AFTER min_hold_time has passed.
    The severe crash emergency exit is handled separately in evaluate_exit_rules.
    """
    # Determine current direction (positive diff means we earn funding)
    current_diff_hourly = (
        current_lighter_rate - current_x10_rate
        if trade.leg1.side == Side.SELL
        else current_x10_rate - current_lighter_rate
    )

    # Convert hourly diff to APY for comparison
    current_diff_apy = current_diff_hourly * Decimal("24") * Decimal("365")

    # If we entered expecting profit (positive APY)
    if trade.entry_apy > 0:
        # Buffer: Only consider exit if APY is meaningfully negative
        threshold_apy = Decimal("-0.05")  # -5% APY buffer to avoid noise

        if current_diff_apy < threshold_apy:
            # Funding has genuinely flipped against us

            # A) If trade is profitable, exit to lock in gains
            if current_pnl > estimated_exit_cost_usd:
                return RuleResult(
                    True, f"Funding flipped ({current_diff_apy:.2%} APY), locking profit +${current_pnl:.2f}"
                )

            # B) Cost-benefit: Only exit if projected loss > exit cost
            # Calculate projected loss over next `hours_threshold` if we hold.
            notional = trade.target_notional_usd
            if notional <= 0 and trade.leg1.filled_qty > 0 and trade.leg1.entry_price > 0:
                notional = abs(trade.leg1.filled_qty * trade.leg1.entry_price)
            if notional <= 0:
                notional = Decimal("0")

            horizon_hours = hours_threshold if hours_threshold and hours_threshold > 0 else Decimal("8")
            projected_loss = abs(current_diff_hourly) * notional * horizon_hours

            if projected_loss > estimated_exit_cost_usd:
                return RuleResult(
                    True,
                    f"Funding Flip: {horizon_hours}h loss (${projected_loss:.2f}) > "
                    f"exit cost (${estimated_exit_cost_usd:.2f})",
                )
            else:
                return RuleResult(
                    False,
                    f"Holding despite flip: exit cost (${estimated_exit_cost_usd:.2f}) > "
                    f"{horizon_hours}h loss (${projected_loss:.2f})",
                )

    return RuleResult(False)


def check_net_ev_exit(
    trade: Trade,
    *,
    current_lighter_rate: Decimal,
    current_x10_rate: Decimal,
    estimated_exit_cost_usd: Decimal,
    horizon_hours: Decimal,
    exit_cost_multiple: Decimal,
) -> RuleResult:
    """
    Exit if the expected net funding over a horizon does not justify staying in the trade.

    - If net funding is negative: exit if projected loss >= exit_cost * multiple.
    - If net funding is positive but too small: exit if projected gain < exit_cost * multiple.
    """
    if horizon_hours <= 0:
        return RuleResult(False)
    if exit_cost_multiple <= 0:
        exit_cost_multiple = Decimal("1.0")

    notional = trade.target_notional_usd
    if notional <= 0 and trade.leg1.filled_qty > 0 and trade.leg1.entry_price > 0:
        notional = abs(trade.leg1.filled_qty * trade.leg1.entry_price)

    # Net funding rate per hour for the position direction
    if trade.leg1.side == Side.BUY:
        # Long Lighter, Short X10 => net = X10 - Lighter
        net_rate_hourly = current_x10_rate - current_lighter_rate
    else:
        # Short Lighter, Long X10 => net = Lighter - X10
        net_rate_hourly = current_lighter_rate - current_x10_rate

    funding_per_hour_usd = net_rate_hourly * notional
    projected = funding_per_hour_usd * horizon_hours
    threshold = estimated_exit_cost_usd * exit_cost_multiple

    if funding_per_hour_usd < 0:
        projected_loss = abs(projected)
        if projected_loss >= threshold:
            return RuleResult(
                True,
                f"NetEV: projected {horizon_hours}h funding loss ${projected_loss:.2f} >= "
                f"exit cost*{exit_cost_multiple} ${threshold:.2f}",
            )
        return RuleResult(
            False,
            f"Hold (NetEV): exit cost*{exit_cost_multiple} ${threshold:.2f} > "
            f"projected {horizon_hours}h loss ${projected_loss:.2f}",
        )

    if projected < threshold:
        return RuleResult(
            True,
            f"NetEV: projected {horizon_hours}h funding ${projected:.2f} < "
            f"exit cost*{exit_cost_multiple} ${threshold:.2f}",
        )

    return RuleResult(
        False,
        f"Hold (NetEV): projected {horizon_hours}h funding ${projected:.2f} >= "
        f"exit cost*{exit_cost_multiple} ${threshold:.2f}",
    )


# REMOVED 2026-01-10: check_apy_crash_smart - Replaced by FundingVelocityExitStrategy
# Reason: APY naturally oscillates 50-150%. Ratio-based exits cause premature closures.
# Professional strategy uses velocity-based proactive detection instead.


def check_yield_vs_cost(
    trade: Trade,
    current_apy: Decimal,
    *,
    estimated_exit_cost_usd: Decimal = Decimal("0"),
    min_hours_to_cover: Decimal = Decimal("24"),
) -> RuleResult:
    """
    Exit if the position cannot cover its exit costs within a reasonable horizon.

    This is the "Bleeder Filter" - prevents holding positions where the yield
    is so low that we'd never recoup the exit fees.

    Formula: hourly_yield = (APY / 8760) * notional
             required_hours = exit_cost / hourly_yield
             EXIT if required_hours > min_hours_to_cover
    """
    if estimated_exit_cost_usd <= 0:
        return RuleResult(False, "No exit cost estimate available")

    notional = trade.target_notional_usd
    if notional <= 0:
        return RuleResult(False, "No notional available for yield calculation")

    # Hourly yield = APY / 8760 hours * notional
    hourly_yield = (current_apy / Decimal("8760")) * notional

    if hourly_yield <= 0:
        return RuleResult(True, f"Yield non-positive ({current_apy:.2%} APY): EXIT")

    hours_to_cover = estimated_exit_cost_usd / hourly_yield

    if hours_to_cover > min_hours_to_cover:
        return RuleResult(
            True,
            f"Unholdable: {hours_to_cover:.1f}h to cover ${estimated_exit_cost_usd:.2f} "
            f"exit > {min_hours_to_cover}h limit",
        )

    return RuleResult(
        False,
        f"Holdable: {hours_to_cover:.1f}h to cover exit (< {min_hours_to_cover}h)",
    )


def check_z_score_crash(
    trade: Trade,
    current_apy: Decimal,
    historical_apy: list[Decimal],
    *,
    z_score_exit_threshold: Decimal = Decimal("-2.0"),
    z_score_emergency_threshold: Decimal = Decimal("-3.0"),
    min_samples: int = 6,
) -> RuleResult:
    """
    Z-Score based APY crash detection.

    Uses statistical deviation from historical mean instead of fixed ratio.
    More adaptive to different market regimes and asset volatility profiles.

    Z-Score = (current - mean) / std_dev

    EXIT if Z-Score <= threshold (default -2.0 = 2 sigma crash)
    EMERGENCY if Z-Score <= emergency_threshold (default -3.0)
    """
    if len(historical_apy) < min_samples:
        return RuleResult(
            False,
            f"Z-Score: Insufficient history ({len(historical_apy)}/{min_samples})",
        )

    # Calculate mean and standard deviation
    n = len(historical_apy)
    mean_apy = sum(historical_apy) / n

    variance = sum((x - mean_apy) ** 2 for x in historical_apy) / n
    std_apy = variance ** Decimal("0.5")

    if std_apy == 0:
        return RuleResult(False, "Z-Score: No variance in historical APY")

    z_score = (current_apy - mean_apy) / std_apy

    # Emergency exit (extreme crash)
    if z_score <= z_score_emergency_threshold:
        return RuleResult(
            True,
            f"Z-SCORE EMERGENCY: {z_score:.2f}σ <= {z_score_emergency_threshold}σ "
            f"(APY {current_apy:.2%}, mean {mean_apy:.2%})",
        )

    # Standard exit (significant crash)
    if z_score <= z_score_exit_threshold:
        return RuleResult(
            True,
            f"Z-SCORE CRASH: {z_score:.2f}σ <= {z_score_exit_threshold}σ (APY {current_apy:.2%}, mean {mean_apy:.2%})",
        )

    return RuleResult(
        False,
        f"Z-Score OK: {z_score:.2f}σ (mean {mean_apy:.2%}, std {std_apy:.2%})",
    )


def check_basis_convergence(
    trade: Trade,
    current_spread_pct: Decimal,
    entry_spread_pct: Decimal,
    current_pnl: Decimal,
    convergence_threshold_pct: Decimal = Decimal("0.0005"),  # 0.05%
    min_convergence_ratio: Decimal = Decimal("0.20"),  # Current < 20% of entry = 80% collapse
    min_profit_usd: Decimal = Decimal("0.50"),
) -> RuleResult:
    """
    Exit when the price spread between exchanges converges to near-zero.

    In Perp-Perp arbitrage, the "basis" is the price spread between exchanges.
    When this spread converges to zero, the arbitrage opportunity is "complete":
    - Arb profit is locked in
    - Continuing to hold is speculation, not arbitrage

    Exit triggers:
    1. Current spread < absolute threshold (e.g., 0.05%)
    2. Current spread < entry_spread * ratio (spread collapsed by 80%+)

    Both require: PnL > min_profit_usd (don't exit at a loss)

    Args:
        trade: The active trade
        current_spread_pct: Current price spread between exchanges (as decimal, e.g., 0.001 = 0.1%)
        entry_spread_pct: Spread at trade entry
        current_pnl: Current unrealized PnL in USD
        convergence_threshold_pct: Absolute spread threshold to trigger exit
        min_convergence_ratio: If current < entry * ratio, trigger exit
        min_profit_usd: Minimum profit required to exit on convergence

    Returns:
        RuleResult with passed=True if convergence exit recommended
    """
    # Safety: Need valid spread data
    if current_spread_pct is None or current_spread_pct < Decimal("0"):
        return RuleResult(False, "Basis: Missing spread data")

    # Don't exit at a loss - only lock in profits
    if current_pnl < min_profit_usd:
        return RuleResult(
            False,
            f"Basis: Spread={current_spread_pct:.4%} but PnL ${current_pnl:.2f} < ${min_profit_usd:.2f}",
        )

    # Check 1: Absolute threshold - spread is near zero
    if current_spread_pct <= convergence_threshold_pct:
        return RuleResult(
            True,
            f"BASIS CONVERGED: Spread {current_spread_pct:.4%} <= {convergence_threshold_pct:.4%} "
            f"(PnL ${current_pnl:.2f}) - Arb Complete",
        )

    # Check 2: Relative collapse - spread dropped significantly from entry
    if entry_spread_pct > Decimal("0"):
        collapse_target = entry_spread_pct * min_convergence_ratio
        if current_spread_pct <= collapse_target:
            collapse_pct = (Decimal("1") - (current_spread_pct / entry_spread_pct)) * Decimal("100")
            return RuleResult(
                True,
                f"SPREAD COLLAPSED: {collapse_pct:.0f}% reduction "
                f"(Entry {entry_spread_pct:.4%} → {current_spread_pct:.4%}, PnL ${current_pnl:.2f})",
            )

    return RuleResult(
        False,
        f"Basis: Spread={current_spread_pct:.4%} (entry={entry_spread_pct:.4%}), holding",
    )


# REMOVED 2026-01-10: check_volatility_panic
# Reason: Market-neutral strategy means price volatility is irrelevant for funding arbitrage.
# We're long one exchange, short the other - price movements affect both legs equally.
# def check_volatility_panic(price_change_pct: Decimal, panic_threshold: Decimal) -> RuleResult:
#     """Check if price volatility exceeds panic threshold."""
#     if abs(price_change_pct) >= panic_threshold:
#         return RuleResult(True, f"Volatility panic: {price_change_pct:.2%} >= {panic_threshold:.2%}")
#     return RuleResult(False)


# REMOVED 2026-01-10: check_trailing_stop - Replaced by ATRTrailingStopStrategy
# Reason: Static percentage trailing stop is volatility-blind. ATR-based is professional.


# =============================================================================
# NEW EXIT STRATEGIES v3.1 LEAN (2026-01-10)
# =============================================================================


def check_catastrophic_funding_flip(
    trade: Trade,
    lighter_rate: Decimal,
    x10_rate: Decimal,
    severe_crash_threshold: Decimal = Decimal("-2.00"),
) -> RuleResult:
    """
    Check for catastrophic funding flip (-200% APY or worse).

    This is an EMERGENCY exit that bypasses min_hold gate.
    Only triggers if we entered with positive APY and now have severe negative APY.

    Args:
        trade: The active trade
        lighter_rate: Current Lighter funding rate (hourly)
        x10_rate: Current X10 funding rate (hourly)
        severe_crash_threshold: APY threshold for catastrophic crash (default -200%)
    """
    if trade.entry_apy <= 0:
        return RuleResult(False, "Entry APY was non-positive, no crash possible")

    # Calculate current funding difference based on position direction
    current_diff_hourly = lighter_rate - x10_rate if trade.leg1.side == Side.SELL else x10_rate - lighter_rate
    current_diff_apy = current_diff_hourly * Decimal("24") * Decimal("365")

    if current_diff_apy < severe_crash_threshold:
        return RuleResult(
            True,
            f"Catastrophic funding flip: {current_diff_apy:.2%} APY (entry was {trade.entry_apy:.2%})",
        )

    return RuleResult(False)


def check_liquidation_distance(
    trade: Trade,
    min_distance_pct: Decimal = Decimal("0.10"),
    leg1_liq_distance_pct: Decimal = Decimal("-1"),
    leg2_liq_distance_pct: Decimal = Decimal("-1"),
) -> RuleResult:
    """
    Check if position is too close to liquidation.

    Monitors both legs of the delta-neutral position.
    Uses liquidation price from exchange position API.

    Args:
        trade: The active trade
        min_distance_pct: Minimum safety buffer (default 10%)
        leg1_liq_distance_pct: Lighter leg distance to liquidation (decimal, e.g., 0.15 = 15%)
        leg2_liq_distance_pct: X10 leg distance to liquidation (decimal, e.g., 0.12 = 12%)
                             -1 indicates liquidation_price is missing

    Returns:
        RuleResult with passed=True if either leg is too close to liquidation

    Exit if:
    - min(leg1_distance, leg2_distance) < min_distance_pct
    - E.g., 10% buffer means exit when position is 90% liquidated
    - If liquidation_price is missing (-1), returns passed=False with clear reason
    """
    # Check for missing liquidation_price data (sentinel value -1)
    leg1_missing = leg1_liq_distance_pct < Decimal("0")
    leg2_missing = leg2_liq_distance_pct < Decimal("0")

    if leg1_missing and leg2_missing:
        return RuleResult(False, f"liquidation_price missing for both legs ({trade.symbol})")
    if leg1_missing:
        return RuleResult(False, f"liquidation_price missing for Lighter leg ({trade.symbol})")
    if leg2_missing:
        return RuleResult(False, f"liquidation_price missing for X10 leg ({trade.symbol})")

    # Find the leg closest to liquidation (worst case)
    worst_distance_pct = min(leg1_liq_distance_pct, leg2_liq_distance_pct)

    if worst_distance_pct < min_distance_pct:
        # Determine which leg triggered the exit
        triggered_leg = "Lighter" if leg1_liq_distance_pct <= leg2_liq_distance_pct else "X10"
        triggered_distance = (
            leg1_liq_distance_pct if leg1_liq_distance_pct <= leg2_liq_distance_pct else leg2_liq_distance_pct
        )
        return RuleResult(
            True,
            f"Liquidation risk: {triggered_leg} leg at {triggered_distance:.1%} from liquidation "
            f"(leg1={leg1_liq_distance_pct:.1%}, leg2={leg2_liq_distance_pct:.1%}) "
            f"< threshold {min_distance_pct:.1%}",
        )

    return RuleResult(
        False,
        f"Liquidation safe: {worst_distance_pct:.1%} distance "
        f"(leg1={leg1_liq_distance_pct:.1%}, leg2={leg2_liq_distance_pct:.1%})",
    )


def check_delta_bound(
    trade: Trade,
    max_delta_pct: Decimal = Decimal("0.03"),
    min_delta_pct: Decimal | None = None,
    leg1_mark_price: Decimal | None = None,
    leg2_mark_price: Decimal | None = None,
) -> RuleResult:
    """
    Check if delta neutrality is violated (>3% delta drift).

    Delta neutrality is CRITICAL for funding arbitrage.
    If one leg drifts significantly, the hedge is broken and we're exposed to directional risk.

    Three-tier behavior (Phase 3):
    - drift < min_delta_pct: OK, log nothing
    - min_delta_pct <= drift <= max_delta_pct: REBALANCE (restore neutrality without full exit)
    - drift > max_delta_pct: EMERGENCY EXIT (full position close)

    Exit if:
    - The absolute delta (position imbalance) exceeds max_delta_pct of total notional
    - E.g., 3% threshold means exit if legs are imbalanced by more than 3%

    Rebalance if:
    - min_delta_pct is provided AND drift is between min and max thresholds
    - E.g., 1-3% means rebalance at 2% drift instead of full exit

    Args:
        trade: The active trade
        max_delta_pct: Maximum allowed delta drift before emergency exit (default 3%)
        min_delta_pct: Minimum delta drift to trigger rebalance (default None = disabled)
        leg1_mark_price: Current mark price for leg1 (Lighter)
        leg2_mark_price: Current mark price for leg2 (X10)

    Returns:
        RuleResult with:
        - passed=True + REBALANCE reason when drift is in rebalance range
        - passed=True + EMERGENCY reason when drift exceeds max
        - passed=False when drift is acceptable

    NOTE: Uses CURRENT mark price for notional calculation, not entry price.
    """
    # Use mark prices if provided, otherwise fall back to entry price
    leg1_px = leg1_mark_price if leg1_mark_price and leg1_mark_price > 0 else trade.leg1.entry_price
    leg2_px = leg2_mark_price if leg2_mark_price and leg2_mark_price > 0 else trade.leg2.entry_price

    # Calculate current notional values for both legs
    leg1_notional = trade.leg1.filled_qty * leg1_px
    leg2_notional = trade.leg2.filled_qty * leg2_px

    if leg1_notional == 0 or leg2_notional == 0:
        return RuleResult(False, "Zero notional (cannot calculate delta)")

    # Apply sign based on side (long = positive, short = negative)
    leg1_delta = leg1_notional if trade.leg1.side == Side.BUY else -leg1_notional
    leg2_delta = leg2_notional if trade.leg2.side == Side.BUY else -leg2_notional

    # Calculate net delta (should be near zero for delta-neutral)
    net_delta = abs(leg1_delta + leg2_delta)
    total_notional = abs(leg1_notional) + abs(leg2_notional)

    if total_notional == 0:
        return RuleResult(False, "Zero total notional")

    # Calculate delta drift as percentage of total notional
    delta_drift_pct = net_delta / total_notional

    # Phase 3: Rebalance layer (between min and max thresholds)
    if min_delta_pct is not None and delta_drift_pct >= min_delta_pct and delta_drift_pct <= max_delta_pct:
        # In rebalance range - restore neutrality without full exit
        return RuleResult(
            True,
            f"{REASON_REBALANCE}: Delta drift {delta_drift_pct:.2%} in rebalance range "
            f"({min_delta_pct:.2%} - {max_delta_pct:.2%}) "
            f"(leg1={leg1_delta:.2f} @ {leg1_px:.2f}, leg2={leg2_delta:.2f} @ {leg2_px:.2f})",
        )

    # Emergency exit layer (exceeds max threshold)
    if delta_drift_pct > max_delta_pct:
        return RuleResult(
            True,
            f"{REASON_EMERGENCY}: Delta bound violated: {delta_drift_pct:.2%} > {max_delta_pct:.2%} "
            f"(leg1_delta={leg1_delta:.2f} @ {leg1_px:.2f}, leg2_delta={leg2_delta:.2f} @ {leg2_px:.2f})",
        )

    return RuleResult(
        False, f"Delta OK: {delta_drift_pct:.2%} <= {max_delta_pct:.2%} (leg1={leg1_delta:.2f}, leg2={leg2_delta:.2f})"
    )


def check_opportunity_cost(
    current_apy: Decimal,
    best_available_apy: Decimal,
    min_diff: Decimal,
    *,
    trade_notional_usd: Decimal | None = None,
    horizon_hours: Decimal | None = None,
    estimated_exit_cost_usd: Decimal = Decimal("0"),
    rotation_roundtrip_multiple: Decimal = Decimal("2.0"),
    rotation_cost_multiple: Decimal = Decimal("1.0"),
) -> RuleResult:
    """
    Exit if a much better opportunity exists.

    When exit-ev mode is active, we make this cost-aware:
    rotate only if the incremental funding over a horizon is expected to cover a
    conservative roundtrip cost (close + re-open) with a safety multiple.
    """
    if best_available_apy <= (current_apy + min_diff):
        return RuleResult(False)

    # Cost-aware rotation (preferred when we have inputs).
    if (
        trade_notional_usd is not None
        and horizon_hours is not None
        and trade_notional_usd > 0
        and horizon_hours > 0
        and estimated_exit_cost_usd > 0
    ):
        if rotation_roundtrip_multiple <= 0:
            rotation_roundtrip_multiple = Decimal("2.0")
        if rotation_cost_multiple <= 0:
            rotation_cost_multiple = Decimal("1.0")

        delta_apy = best_available_apy - current_apy
        hours_per_year = Decimal("24") * Decimal("365")
        projected_gain_usd = trade_notional_usd * delta_apy * (horizon_hours / hours_per_year)

        projected_roundtrip_cost_usd = estimated_exit_cost_usd * rotation_roundtrip_multiple
        threshold_usd = projected_roundtrip_cost_usd * rotation_cost_multiple

        if projected_gain_usd >= threshold_usd:
            return RuleResult(
                True,
                (
                    f"Opportunity Rotation: projected {horizon_hours}h gain ${projected_gain_usd:.2f} "
                    f">= est roundtrip cost*{rotation_cost_multiple} ${threshold_usd:.2f} "
                    f"(APY {best_available_apy:.1%} vs {current_apy:.1%})"
                ),
            )
        return RuleResult(False)

    # Fallback: APY-only rotation (legacy behavior)
    return RuleResult(
        True,
        f"Opportunity Cost: New APY {best_available_apy:.1%} > Current {current_apy:.1%} + {min_diff:.1%}",
    )


# =============================================================================
# Composite Rule Checks
# =============================================================================

# NOTE: EntryDecision and evaluate_entry_rules were removed as unused.
# Entry logic is now in OpportunityEngine.validate_opportunity().


@dataclass
class ExitDecision:
    """Result of evaluating exit rules."""

    should_exit: bool
    reason: str


@dataclass
class ExitRulesContext:
    """
    Context object for exit rule evaluation.

    Groups 60+ parameters into logical categories for maintainability.
    Use ExitRulesContext.from_settings() to construct from TradingSettings.
    """

    # === CORE PARAMETERS ===
    trade: Trade
    current_pnl: Decimal
    current_apy: Decimal
    lighter_rate: Decimal
    x10_rate: Decimal
    price_pnl: Decimal | None = None

    # === TIME CONSTRAINTS ===
    min_hold_seconds: int = 172800  # 48h default
    max_hold_hours: Decimal = Decimal("240")

    # === PROFIT TARGETS ===
    profit_target_usd: Decimal = Decimal("0")
    funding_flip_hours: Decimal = Decimal("6.0")
    high_water_mark: Decimal = Decimal("0")
    estimated_exit_cost_usd: Decimal = Decimal("0")

    # === OPPORTUNITY COST ===
    best_opportunity_apy: Decimal = Decimal("0")
    opportunity_cost_diff: Decimal = Decimal("0.40")

    # === EARLY EXIT SETTINGS ===
    early_take_profit_enabled: bool = False
    early_take_profit_net_usd: Decimal = Decimal("0")
    early_take_profit_slippage_multiple: Decimal = Decimal("1.5")
    early_take_profit_execution_buffer_usd: Decimal = Decimal("0")
    early_edge_exit_enabled: bool = False
    early_edge_exit_min_age_seconds: int = 3600

    # === NET EV EXIT SETTINGS ===
    exit_ev_enabled: bool = False
    exit_ev_horizon_hours: Decimal = Decimal("12.0")
    exit_ev_exit_cost_multiple: Decimal = Decimal("1.4")
    exit_ev_skip_profit_target_when_edge_good: bool = True
    exit_ev_skip_opportunity_cost_when_edge_good: bool = True

    # === HISTORICAL CONTEXT ===
    historical_crash_count: int = 0
    historical_avg_recovery_minutes: int = 0
    current_crash_depth_pct: Decimal | None = None
    predicted_recovery_p50_minutes: int | None = None
    max_hold_extend_on_recovery_enabled: bool = False

    # === Z-SCORE EXIT (Fallback for old trades) ===
    z_score_exit_enabled: bool = False
    z_score_exit_threshold: Decimal = Decimal("-2.0")
    z_score_emergency_threshold: Decimal = Decimal("-3.0")
    z_score_exit_min_age_hours: int = 168  # 7 days
    historical_apy: list[Decimal] | None = None

    # === YIELD VS COST EXIT ===
    yield_cost_exit_enabled: bool = False
    yield_cost_max_hours: Decimal = Decimal("24.0")

    # === BASIS CONVERGENCE EXIT ===
    basis_convergence_exit_enabled: bool = False
    basis_convergence_threshold_pct: Decimal = Decimal("0.0005")
    basis_convergence_ratio: Decimal = Decimal("0.20")
    basis_convergence_min_profit_usd: Decimal = Decimal("0.50")
    current_spread_pct: Decimal | None = None
    entry_spread_pct: Decimal = Decimal("0")

    # === RISK PARAMETERS (Layer 1: Emergency) ===
    liquidation_distance_monitoring_enabled: bool = False
    liquidation_distance_min_pct: Decimal = Decimal("0.10")
    leg1_liq_distance_pct: Decimal = Decimal("-1")  # -1 = missing
    leg2_liq_distance_pct: Decimal = Decimal("-1")
    leg1_mark_price: Decimal | None = None
    leg2_mark_price: Decimal | None = None
    delta_bound_enabled: bool = True
    delta_bound_max_delta_pct: Decimal = Decimal("0.03")

    # === REBALANCE PARAMETERS (Phase 3) ===
    rebalance_enabled: bool = True
    rebalance_min_delta_pct: Decimal = Decimal("0.01")
    rebalance_max_delta_pct: Decimal = Decimal("0.03")

    # === ATR TRAILING STOP (Layer 2) ===
    atr_trailing_enabled: bool = True
    atr_period: int = 14
    atr_multiplier: Decimal = Decimal("2.0")
    atr_min_activation_usd: Decimal = Decimal("3.0")

    # === FUNDING VELOCITY EXIT (Layer 3) ===
    funding_velocity_exit_enabled: bool = True
    velocity_threshold_hourly: Decimal = Decimal("-0.0015")
    acceleration_threshold: Decimal = Decimal("-0.0008")
    velocity_lookback_hours: int = 6


def evaluate_exit_rules_from_context(ctx: ExitRulesContext) -> ExitDecision:
    """
    Evaluate exit rules using a context object.

    This is a wrapper around evaluate_exit_rules that accepts a single
    ExitRulesContext object instead of 60+ individual parameters.
    Improves maintainability and enables typed parameter passing.
    """
    return evaluate_exit_rules(
        trade=ctx.trade,
        current_pnl=ctx.current_pnl,
        current_apy=ctx.current_apy,
        lighter_rate=ctx.lighter_rate,
        x10_rate=ctx.x10_rate,
        min_hold_seconds=ctx.min_hold_seconds,
        max_hold_hours=ctx.max_hold_hours,
        profit_target_usd=ctx.profit_target_usd,
        funding_flip_hours=ctx.funding_flip_hours,
        high_water_mark=ctx.high_water_mark,
        best_opportunity_apy=ctx.best_opportunity_apy,
        opportunity_cost_diff=ctx.opportunity_cost_diff,
        estimated_exit_cost_usd=ctx.estimated_exit_cost_usd,
        price_pnl=ctx.price_pnl,
        early_take_profit_enabled=ctx.early_take_profit_enabled,
        early_take_profit_net_usd=ctx.early_take_profit_net_usd,
        early_take_profit_slippage_multiple=ctx.early_take_profit_slippage_multiple,
        early_take_profit_execution_buffer_usd=ctx.early_take_profit_execution_buffer_usd,
        early_edge_exit_enabled=ctx.early_edge_exit_enabled,
        early_edge_exit_min_age_seconds=ctx.early_edge_exit_min_age_seconds,
        exit_ev_enabled=ctx.exit_ev_enabled,
        exit_ev_horizon_hours=ctx.exit_ev_horizon_hours,
        exit_ev_exit_cost_multiple=ctx.exit_ev_exit_cost_multiple,
        exit_ev_skip_profit_target_when_edge_good=ctx.exit_ev_skip_profit_target_when_edge_good,
        exit_ev_skip_opportunity_cost_when_edge_good=ctx.exit_ev_skip_opportunity_cost_when_edge_good,
        historical_crash_count=ctx.historical_crash_count,
        historical_avg_recovery_minutes=ctx.historical_avg_recovery_minutes,
        current_crash_depth_pct=ctx.current_crash_depth_pct,
        predicted_recovery_p50_minutes=ctx.predicted_recovery_p50_minutes,
        max_hold_extend_on_recovery_enabled=ctx.max_hold_extend_on_recovery_enabled,
        z_score_exit_enabled=ctx.z_score_exit_enabled,
        z_score_exit_threshold=ctx.z_score_exit_threshold,
        z_score_emergency_threshold=ctx.z_score_emergency_threshold,
        z_score_exit_min_age_hours=ctx.z_score_exit_min_age_hours,
        historical_apy=ctx.historical_apy,
        yield_cost_exit_enabled=ctx.yield_cost_exit_enabled,
        yield_cost_max_hours=ctx.yield_cost_max_hours,
        basis_convergence_exit_enabled=ctx.basis_convergence_exit_enabled,
        basis_convergence_threshold_pct=ctx.basis_convergence_threshold_pct,
        basis_convergence_ratio=ctx.basis_convergence_ratio,
        basis_convergence_min_profit_usd=ctx.basis_convergence_min_profit_usd,
        current_spread_pct=ctx.current_spread_pct,
        entry_spread_pct=ctx.entry_spread_pct,
        liquidation_distance_monitoring_enabled=ctx.liquidation_distance_monitoring_enabled,
        liquidation_distance_min_pct=ctx.liquidation_distance_min_pct,
        leg1_liq_distance_pct=ctx.leg1_liq_distance_pct,
        leg2_liq_distance_pct=ctx.leg2_liq_distance_pct,
        leg1_mark_price=ctx.leg1_mark_price,
        leg2_mark_price=ctx.leg2_mark_price,
        delta_bound_enabled=ctx.delta_bound_enabled,
        delta_bound_max_delta_pct=ctx.delta_bound_max_delta_pct,
        rebalance_enabled=ctx.rebalance_enabled,
        rebalance_min_delta_pct=ctx.rebalance_min_delta_pct,
        rebalance_max_delta_pct=ctx.rebalance_max_delta_pct,
        atr_trailing_enabled=ctx.atr_trailing_enabled,
        atr_period=ctx.atr_period,
        atr_multiplier=ctx.atr_multiplier,
        atr_min_activation_usd=ctx.atr_min_activation_usd,
        funding_velocity_exit_enabled=ctx.funding_velocity_exit_enabled,
        velocity_threshold_hourly=ctx.velocity_threshold_hourly,
        acceleration_threshold=ctx.acceleration_threshold,
        velocity_lookback_hours=ctx.velocity_lookback_hours,
    )


def evaluate_exit_rules(
    trade: Trade,
    current_pnl: Decimal,
    current_apy: Decimal,
    lighter_rate: Decimal,
    x10_rate: Decimal,
    min_hold_seconds: int,
    max_hold_hours: Decimal,
    profit_target_usd: Decimal,
    funding_flip_hours: Decimal,
    # New Dynamic Params
    high_water_mark: Decimal = Decimal("0"),
    best_opportunity_apy: Decimal = Decimal("0"),
    opportunity_cost_diff: Decimal = Decimal("0.40"),
    estimated_exit_cost_usd: Decimal = Decimal("0"),
    price_pnl: Decimal | None = None,
    # Early Take Profit (optional; bypass min_hold if net PnL meets threshold)
    early_take_profit_enabled: bool = False,
    early_take_profit_net_usd: Decimal = Decimal("0"),
    # Slippage buffer for multi-attempt close: effective_threshold = net_usd + (exit_cost * slippage_multiple)
    early_take_profit_slippage_multiple: Decimal = Decimal("1.5"),
    # Additional execution/latency buffer (e.g. taker-padding / market-close drift) to avoid false-positive early exits.
    early_take_profit_execution_buffer_usd: Decimal = Decimal("0"),
    # Early Edge Exit (optional; bypass min_hold on edge collapse)
    early_edge_exit_enabled: bool = False,
    early_edge_exit_min_age_seconds: int = 3600,
    # Net EV Exit (optional)
    exit_ev_enabled: bool = False,
    exit_ev_horizon_hours: Decimal = Decimal("12.0"),
    exit_ev_exit_cost_multiple: Decimal = Decimal("1.4"),
    exit_ev_skip_profit_target_when_edge_good: bool = True,
    exit_ev_skip_opportunity_cost_when_edge_good: bool = True,
    # Historical crash context parameters (still used for max hold extension)
    historical_crash_count: int = 0,
    historical_avg_recovery_minutes: int = 0,
    current_crash_depth_pct: Decimal | None = None,
    predicted_recovery_p50_minutes: int | None = None,
    # Max hold extension on recovery prediction
    max_hold_extend_on_recovery_enabled: bool = False,
    # Z-Score Exit (Phase 1 Holy Grail) - ONLY for older trades as fallback
    z_score_exit_enabled: bool = False,
    z_score_exit_threshold: Decimal = Decimal("-2.0"),
    z_score_emergency_threshold: Decimal = Decimal("-3.0"),
    z_score_exit_min_age_hours: int = 168,  # Only trigger for trades 7+ days old
    historical_apy: list[Decimal] | None = None,
    # Yield vs. Cost Exit (Phase 1 Holy Grail)
    yield_cost_exit_enabled: bool = False,
    yield_cost_max_hours: Decimal = Decimal("24.0"),
    # Basis Convergence Exit (Phase 2 Holy Grail)
    basis_convergence_exit_enabled: bool = False,
    basis_convergence_threshold_pct: Decimal = Decimal("0.0005"),
    basis_convergence_ratio: Decimal = Decimal("0.20"),
    basis_convergence_min_profit_usd: Decimal = Decimal("0.50"),
    current_spread_pct: Decimal | None = None,
    entry_spread_pct: Decimal = Decimal("0"),
    # NEW v3.1 LEAN: Emergency Exits (Layer 1)
    liquidation_distance_monitoring_enabled: bool = False,
    liquidation_distance_min_pct: Decimal = Decimal("0.10"),
    # NEW: Liquidation distance per leg (calculated in exit_eval)
    # -1 indicates liquidation_price is missing (sentinel value)
    leg1_liq_distance_pct: Decimal = Decimal("-1"),
    leg2_liq_distance_pct: Decimal = Decimal("-1"),
    # NEW: Current mark prices per leg (for delta_bound fix)
    leg1_mark_price: Decimal | None = None,
    leg2_mark_price: Decimal | None = None,
    delta_bound_enabled: bool = True,
    delta_bound_max_delta_pct: Decimal = Decimal("0.03"),
    # Phase 3: Rebalance parameters
    rebalance_enabled: bool = True,
    rebalance_min_delta_pct: Decimal = Decimal("0.01"),
    rebalance_max_delta_pct: Decimal = Decimal("0.03"),
    # NEW v3.1 LEAN: ATR Trailing Stop (Layer 2)
    atr_trailing_enabled: bool = True,
    atr_period: int = 14,
    atr_multiplier: Decimal = Decimal("2.0"),
    atr_min_activation_usd: Decimal = Decimal("3.0"),
    # NEW v3.1 LEAN: Funding Velocity Exit (Layer 3)
    funding_velocity_exit_enabled: bool = True,
    velocity_threshold_hourly: Decimal = Decimal("-0.0015"),
    acceleration_threshold: Decimal = Decimal("-0.0008"),
    velocity_lookback_hours: int = 6,
) -> ExitDecision:
    """
    Evaluate exit rules with STRICT priority order (first-hit-wins).

    PRIORITY 1 (EMERGENCY): Override everything, including min_hold
    - Liquidation Distance Monitor (if enabled and adapter supports it)
    - Delta Bound Violation (3% max delta drift)
    - Catastrophic Funding Flip (-200% APY)
    - Early Take Profit (price dislocation bypass)

    GATE 1: Min Hold Time (48h) - blocks ALL non-emergency exits

    GATE 2: Max Hold Time (240h) - forces exit regardless

    PRIORITY 2 (ECONOMIC): Cost-aware exits
    - Funding Flip (standard)
    - Netto EV Exit (1.4x exit cost multiple)
    - Yield vs Cost Exit (24h to cover)
    - Basis Convergence Exit (spread lock-in)

    PRIORITY 3 (OPTIMIZATION): Profit maximization
    - Funding Velocity Exit (proactive crash detection)
    - Z-Score Exit (statistical fallback)
    - Opportunity Cost Rotation

    Default: Hold (no exit conditions met)
    """

    # =========================================================================
    # PRIORITY 1: EMERGENCY EXITS (Override min_hold gate)
    # =========================================================================

    # 1.1 Liquidation Distance Monitor (if enabled and adapter supports it)
    if liquidation_distance_monitoring_enabled:
        liq_result = check_liquidation_distance(
            trade,
            min_distance_pct=liquidation_distance_min_pct,
            leg1_liq_distance_pct=leg1_liq_distance_pct,
            leg2_liq_distance_pct=leg2_liq_distance_pct,
        )
        if liq_result.passed:
            return ExitDecision(True, f"{REASON_EMERGENCY}: {liq_result.reason}")

    # 1.2 Delta Bound Violation (hedge protection)
    if delta_bound_enabled:
        # Phase 3: Pass min_delta_pct to enable rebalance layer
        min_pct = rebalance_min_delta_pct if rebalance_enabled else None
        delta_result = check_delta_bound(
            trade,
            max_delta_pct=delta_bound_max_delta_pct,
            min_delta_pct=min_pct,
            leg1_mark_price=leg1_mark_price,
            leg2_mark_price=leg2_mark_price,
        )
        if delta_result.passed:
            # Check if this is a rebalance or emergency exit
            if delta_result.reason.startswith(REASON_REBALANCE):
                return ExitDecision(True, delta_result.reason)  # REBALANCE: No EMERGENCY prefix
            return ExitDecision(True, f"{REASON_EMERGENCY}: {delta_result.reason}")

    # 1.3 Catastrophic Funding Flip (-200% APY)
    catastrophic_result = check_catastrophic_funding_flip(trade, lighter_rate, x10_rate)
    if catastrophic_result.passed:
        logger.warning(f"CATASTROPHIC FUNDING FLIP for {trade.symbol} - EMERGENCY EXIT")
        return ExitDecision(True, f"{REASON_EMERGENCY}: {catastrophic_result.reason}")

    # 1.4 Early Take Profit (price dislocation - bypasses min_hold)
    if early_take_profit_enabled and early_take_profit_net_usd > 0:
        if price_pnl is None:
            price_pnl = current_pnl
        slippage_buffer = estimated_exit_cost_usd * early_take_profit_slippage_multiple
        execution_buffer = max(early_take_profit_execution_buffer_usd, Decimal("0"))
        effective_threshold = early_take_profit_net_usd + max(slippage_buffer, Decimal("0.50")) + execution_buffer
        if price_pnl >= effective_threshold and current_pnl >= 0:
            buffer_usd = max(slippage_buffer, Decimal("0.50")) + execution_buffer
            return ExitDecision(
                True,
                f"{REASON_EARLY_TP}: price PnL ${price_pnl:.2f} >= ${effective_threshold:.2f} "
                f"(base ${early_take_profit_net_usd:.2f} + buffer ${buffer_usd:.2f})",
            )

    # 1.5 Early Edge Exit (funding flip - bypasses min_hold after age gate)
    if early_edge_exit_enabled and trade.hold_duration_seconds >= max(0, int(early_edge_exit_min_age_seconds or 0)):
        early_flip = check_funding_flip(
            trade, lighter_rate, x10_rate, funding_flip_hours, current_pnl, estimated_exit_cost_usd
        )
        if early_flip.passed:
            return ExitDecision(True, f"{REASON_EARLY_EDGE}: {early_flip.reason}")

    # =========================================================================
    # GATE 1: MINIMUM HOLD TIME (Blocks all non-emergency exits)
    # =========================================================================
    min_hold = check_min_hold_time(trade, min_hold_seconds)
    if not min_hold.passed:
        return ExitDecision(False, min_hold.reason)

    # =========================================================================
    # GATE 2: MAXIMUM HOLD TIME (Forces exit regardless)
    # =========================================================================
    max_hold_extend_enabled = max_hold_extend_on_recovery_enabled
    max_hold = check_max_hold_time(
        trade,
        max_hold_hours,
        extend_on_recovery_prediction=max_hold_extend_enabled,
        predicted_recovery_p50_minutes=predicted_recovery_p50_minutes,
    )
    if max_hold.passed:
        return ExitDecision(True, max_hold.reason)

    # =========================================================================
    # PRIORITY 2: ECONOMIC EXITS (Cost-aware)
    # =========================================================================

    # 2.1 Funding Flip (standard)
    flip = check_funding_flip(trade, lighter_rate, x10_rate, funding_flip_hours, current_pnl, estimated_exit_cost_usd)
    if flip.passed:
        return ExitDecision(True, flip.reason)

    # 2.2 Netto EV Exit
    hold_edge_good = False
    netev_reason = ""
    if exit_ev_enabled:
        netev = check_net_ev_exit(
            trade,
            current_lighter_rate=lighter_rate,
            current_x10_rate=x10_rate,
            estimated_exit_cost_usd=estimated_exit_cost_usd,
            horizon_hours=exit_ev_horizon_hours,
            exit_cost_multiple=exit_ev_exit_cost_multiple,
        )
        netev_reason = netev.reason
        if netev.passed:
            return ExitDecision(True, netev.reason)
        if netev.reason.startswith("Hold (NetEV):"):
            hold_edge_good = True

    # 2.3 Yield vs Cost Exit
    if yield_cost_exit_enabled:
        yc_result = check_yield_vs_cost(
            trade,
            current_apy,
            estimated_exit_cost_usd=estimated_exit_cost_usd,
            min_hours_to_cover=yield_cost_max_hours,
        )
        if yc_result.passed:
            return ExitDecision(True, f"{REASON_YIELD_COST}: {yc_result.reason}")

    # 2.4 Basis Convergence Exit
    if basis_convergence_exit_enabled and current_spread_pct is not None:
        bc_result = check_basis_convergence(
            trade,
            current_spread_pct=current_spread_pct,
            entry_spread_pct=entry_spread_pct,
            current_pnl=current_pnl,
            convergence_threshold_pct=basis_convergence_threshold_pct,
            min_convergence_ratio=basis_convergence_ratio,
            min_profit_usd=basis_convergence_min_profit_usd,
        )
        if bc_result.passed:
            return ExitDecision(True, f"{REASON_BASIS}: {bc_result.reason}")

    # =========================================================================
    # PRIORITY 3: OPTIMIZATION EXITS (Profit maximization)
    # =========================================================================

    # 3.1 Funding Velocity Exit (proactive crash detection)
    if funding_velocity_exit_enabled and historical_apy and len(historical_apy) >= velocity_lookback_hours:
        from funding_bot.domain.professional_exits import FundingVelocityExitStrategy

        velocity_strategy = FundingVelocityExitStrategy(
            velocity_threshold=velocity_threshold_hourly,
            acceleration_threshold=acceleration_threshold,
            lookback_hours=velocity_lookback_hours,
        )
        velocity_result = velocity_strategy.evaluate(funding_rates=historical_apy)
        if velocity_result.should_exit:
            return ExitDecision(True, f"{REASON_VELOCITY}: {velocity_result.reason}")

    # 3.2 Z-Score Exit (fallback for older trades ONLY - 7+ days)
    # Weakened: Only triggers for trades older than z_score_exit_min_age_hours
    # New trades should use FundingVelocityExit (proactive) instead
    if z_score_exit_enabled and historical_apy:
        trade_age_hours = 0.0
        if trade.opened_at:
            trade_age_hours = (datetime.now(UTC) - trade.opened_at).total_seconds() / 3600

        if trade_age_hours >= z_score_exit_min_age_hours:
            z_result = check_z_score_crash(
                trade,
                current_apy,
                historical_apy,
                z_score_exit_threshold=z_score_exit_threshold,
                z_score_emergency_threshold=z_score_emergency_threshold,
            )
            if z_result.passed:
                return ExitDecision(
                    True,
                    f"{REASON_Z_SCORE} (Fallback for {trade_age_hours:.0f}h old trade): {z_result.reason}",
                )
        # else: Trade too young for Z-Score fallback (prefer FundingVelocityExit)

    # 3.3 Profit Target (conditional - skip if NetEV edge is good)
    if not (exit_ev_enabled and exit_ev_skip_profit_target_when_edge_good and hold_edge_good):
        profit = check_profit_target(trade, current_pnl, profit_target_usd)
        if profit.passed:
            return ExitDecision(True, profit.reason)

    # 3.4 Opportunity Cost Rotation (conditional)
    if not (exit_ev_enabled and exit_ev_skip_opportunity_cost_when_edge_good and hold_edge_good):
        opp_cost = check_opportunity_cost(
            current_apy,
            best_opportunity_apy,
            opportunity_cost_diff,
            trade_notional_usd=trade.target_notional_usd,
            horizon_hours=(exit_ev_horizon_hours if exit_ev_enabled else None),
            estimated_exit_cost_usd=estimated_exit_cost_usd,
            rotation_cost_multiple=(exit_ev_exit_cost_multiple if exit_ev_enabled else Decimal("1.0")),
        )
        if opp_cost.passed:
            return ExitDecision(True, opp_cost.reason)

    # Default hold
    return ExitDecision(False, netev_reason if exit_ev_enabled and netev_reason else "Hold: No exit conditions met")
