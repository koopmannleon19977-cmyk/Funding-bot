"""
Position imbalance checks.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from funding_bot.domain.models import Position, Side, Trade, TradeStatus
from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


async def _check_position_imbalance(self, trade: Trade) -> None:
    """
    Check for position imbalance between exchanges.

    Logs a WARNING when net exposure exceeds 1% of target position.
    Inspired by Treadfi Delta Neutral Bot "Unbalanced" status.

    This is informational only - no automatic action is taken.
    """
    if trade.status != TradeStatus.OPEN or not trade.opened_at:
        return

    # Skip check for very new trades (still settling)
    now = datetime.now(UTC)
    if (now - trade.opened_at).total_seconds() < 60:
        return

    # Skip if target qty is zero (shouldn't happen)
    if trade.target_qty <= Decimal("0"):
        return

    try:
        # Parallel fetch for speed
        lighter_pos, x10_pos = await asyncio.gather(
            self.lighter.get_position(trade.symbol),
            self.x10.get_position(trade.symbol),
            return_exceptions=True
        )

        # Handle exceptions from gather
        if isinstance(lighter_pos, Exception):
            lighter_pos = None
        if isinstance(x10_pos, Exception):
            x10_pos = None
    except Exception:
        return  # Silently skip if positions can't be fetched

    lighter_qty = lighter_pos.qty if lighter_pos else Decimal("0")
    x10_qty = x10_pos.qty if x10_pos else Decimal("0")

    # Calculate imbalance as percentage of target
    imbalance = abs(lighter_qty - x10_qty)
    imbalance_pct = (imbalance / trade.target_qty) * Decimal("100") if trade.target_qty > 0 else Decimal("0")

    # Treadfi threshold: >1% = Unbalanced warning
    if imbalance_pct > Decimal("1"):
        logger.warning(
            f"[UNBALANCED] {trade.symbol}: "
            f"Lighter={lighter_qty:.6f}, X10={x10_qty:.6f}, "
            f"Diff={imbalance:.6f} ({imbalance_pct:.2f}% of target={trade.target_qty:.6f})"
        )


def log_delta_imbalance(
    trade: Trade,
    leg1_position: Position,
    leg2_position: Position,
    leg1_mark_price: Decimal,
    leg2_mark_price: Decimal,
) -> None:
    """
    Log delta imbalance (drift from delta-neutral) for monitoring.

    This is called from exit_eval when live positions are fetched for
    liquidation distance monitoring. It calculates the actual delta drift
    using current mark prices and logs a warning if it exceeds the threshold.

    Args:
        trade: The trade with target quantities and leg sides
        leg1_position: Live position from Lighter
        leg2_position: Live position from X10
        leg1_mark_price: Current mark price for Lighter leg
        leg2_mark_price: Current mark price for X10 leg
    """
    if not leg1_position or not leg2_position:
        return

    # Calculate notional values using current mark prices
    leg1_qty = leg1_position.qty
    leg2_qty = leg2_position.qty

    if leg1_mark_price <= 0 or leg2_mark_price <= 0:
        return

    leg1_notional = leg1_qty * leg1_mark_price
    leg2_notional = leg2_qty * leg2_mark_price
    total_notional = leg1_notional + leg2_notional

    if total_notional <= 0:
        return

    # Calculate delta (direction-aware)
    # Long = positive delta, Short = negative delta
    leg1_delta = leg1_notional if trade.leg1.side == Side.BUY else -leg1_notional
    leg2_delta = leg2_notional if trade.leg2.side == Side.BUY else -leg2_notional
    net_delta = leg1_delta + leg2_delta

    # Delta drift as percentage of total notional
    delta_drift_pct = abs(net_delta / total_notional)

    # Get threshold from settings (default 3%)
    delta_bound_threshold = Decimal("0.03")  # 3%

    if delta_drift_pct > delta_bound_threshold:
        logger.warning(
            f"[DELTA_DRIFT] {trade.symbol}: "
            f"Net Delta=${net_delta:.2f} ({delta_drift_pct:.2%} of ${total_notional:.0f}), "
            f"L1={leg1_delta:+.2f}, L2={leg2_delta:+.2f}"
        )
