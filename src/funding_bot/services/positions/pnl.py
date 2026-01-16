"""
PnL calculations for position management.
"""

from __future__ import annotations

from decimal import Decimal

from funding_bot.domain.models import Side, Trade
from funding_bot.services.market_data import OrderbookDepthSnapshot, OrderbookSnapshot


async def _calculate_unrealized_pnl(
    self, trade: Trade, current_price: Decimal
) -> Decimal:
    """
    Calculate unrealized PnL based on current price (excluding fees).

    Formula:
    - leg1_pnl = (current_price - entry_price) * filled_qty (or inverted for SELL)
    - leg2_pnl = (current_price - entry_price) * filled_qty (or inverted for SELL)
    - gross_pnl = leg1_pnl + leg2_pnl
    - net_pnl = gross_pnl - total_fees

    IMPORTANT: Fees ARE included in this calculation (subtracted at end).
    For gross PnL (before fees), add total_fees back to the result.

    Args:
        trade: The trade to calculate unrealized PnL for
        current_price: Current market price (same for both legs in delta-neutral)

    Returns:
        Decimal: Net unrealized PnL (gross PnL minus total fees)
    """
    # Leg 1 PnL
    if trade.leg1.side == Side.BUY:
        leg1_pnl = (current_price - trade.leg1.entry_price) * trade.leg1.filled_qty
    else:
        leg1_pnl = (trade.leg1.entry_price - current_price) * trade.leg1.filled_qty

    # Leg 2 PnL (opposite side)
    if trade.leg2.side == Side.BUY:
        leg2_pnl = (current_price - trade.leg2.entry_price) * trade.leg2.filled_qty
    else:
        leg2_pnl = (trade.leg2.entry_price - current_price) * trade.leg2.filled_qty

    # Subtract fees
    total_fees = trade.leg1.fees + trade.leg2.fees

    return leg1_pnl + leg2_pnl - total_fees


async def _calculate_realizable_pnl(
    self, trade: Trade, book: OrderbookSnapshot
) -> Decimal:
    """
    Calculate PnL based on realizable prices (Bid/Ask) including fees.

    This accounts for the spread cost of exiting the position.

    Formula:
    - leg1_pnl = (exit_price - entry_price) * filled_qty (using bid/ask based on side)
    - leg2_pnl = (exit_price - entry_price) * filled_qty (using bid/ask based on side)
    - gross_pnl = leg1_pnl + leg2_pnl
    - net_pnl = gross_pnl - total_fees

    IMPORTANT: Fees ARE included in this calculation (subtracted at end).
    For gross PnL (before fees), add total_fees back to the result.

    Args:
        trade: The trade to calculate realizable PnL for
        book: Orderbook snapshot with bid/ask prices for both exchanges

    Returns:
        Decimal: Net realizable PnL (gross PnL minus total fees)
    """
    # Leg 1: Lighter
    if trade.leg1.side == Side.BUY:
        # We are Long Lighter -> We must SELL at Bid
        leg1_pnl = (book.lighter_bid - trade.leg1.entry_price) * trade.leg1.filled_qty
    else:
        # We are Short Lighter -> We must BUY at Ask
        leg1_pnl = (trade.leg1.entry_price - book.lighter_ask) * trade.leg1.filled_qty

    # Leg 2: X10
    if trade.leg2.side == Side.BUY:
        # We are Long X10 -> We must SELL at Bid
        leg2_pnl = (book.x10_bid - trade.leg2.entry_price) * trade.leg2.filled_qty
    else:
        # We are Short X10 -> We must BUY at Ask
        leg2_pnl = (trade.leg2.entry_price - book.x10_ask) * trade.leg2.filled_qty

    # Subtract fees
    total_fees = trade.leg1.fees + trade.leg2.fees

    return leg1_pnl + leg2_pnl - total_fees


async def _calculate_realizable_pnl_depth(
    self, trade: Trade, book: OrderbookDepthSnapshot
) -> Decimal:
    """
    Calculate PnL by walking the orderbook depth (weighted average exit price) including fees.

    Accounts for slippage on large positions by walking the orderbook depth.

    Formula:
    - leg1_pnl = revenue - entry_cost (or entry_revenue - exit_cost)
    - leg2_pnl = revenue - entry_cost (or entry_revenue - exit_cost)
    - gross_pnl = leg1_pnl + leg2_pnl
    - net_pnl = gross_pnl - total_fees

    IMPORTANT: Fees ARE included in this calculation (subtracted at end).
    For gross PnL (before fees), add total_fees back to the result.

    Args:
        trade: The trade to calculate realizable PnL for
        book: Orderbook depth snapshot with bid/ask levels for both exchanges

    Returns:
        Decimal: Net realizable PnL (gross PnL minus total fees)
    """
    # --- Helper to calculate proceeds from walking the book ---
    def _calculate_outcome_usd(
        qty: Decimal,
        levels: list[tuple[Decimal, Decimal]],  # [(price, qty), ...]
        default_price: Decimal,  # Fallback if depth exhausted
    ) -> Decimal:
        remaining = qty
        total_usd = Decimal("0")

        for price, volume in levels:
            if remaining <= 0:
                break

            # Take liquidity from this level
            fill_qty = min(remaining, volume)
            total_usd += fill_qty * price
            remaining -= fill_qty

        # If depth exhausted, assume remainder fills at default_price (or worst seen?)
        # Conservative: use default_price (likely entry price or 0 to penalize).
        # Here we follow existing logic: if default_price is 0, PnL will be awful (good).
        if remaining > 0:
            total_usd += remaining * default_price

        return total_usd

    # --- Leg 1 (Lighter) ---
    # Long -> Sell at Bids
    # Short -> Buy at Asks

    if trade.leg1.side == Side.BUY:
        # Long Exit: Sell Lighter @ Bids
        # Revenue = Sum(Price * Qty)
        # If book empty, use entry price (0 PnL) or 0 (panic)?
        # Existing logic uses entry_price fallback for unrealized.
        # But for "Realizable", if book is empty, we should probably assume bad outcome.
        # Using 0.0 -> massive loss -> prevents exit. Correct.
        revenue = _calculate_outcome_usd(trade.leg1.filled_qty, book.lighter_bids, Decimal("0"))
        # PnL = Exit Revenue - Entry Cost
        # Entry Cost = Entry Price * Qty
        entry_cost = trade.leg1.entry_price * trade.leg1.filled_qty
        leg1_pnl = revenue - entry_cost
    else:
        # Short Exit: Buy Lighter @ Asks
        # Cost = Sum(Price * Qty)
        cost = _calculate_outcome_usd(trade.leg1.filled_qty, book.lighter_asks, Decimal("999999"))
        if cost >= Decimal("999999"):  # Depth exhausted/empty
            # Penalize to prevent exit
            cost = trade.leg1.entry_price * trade.leg1.filled_qty * Decimal("2")

        # PnL = Entry Revenue - Exit Cost
        entry_revenue = trade.leg1.entry_price * trade.leg1.filled_qty
        leg1_pnl = entry_revenue - cost

    # --- Leg 2 (X10) ---
    # Long -> Sell at Bids
    # Short -> Buy at Asks
    if trade.leg2.side == Side.BUY:
        # Long Exit: Sell X10 @ Bids
        revenue = _calculate_outcome_usd(trade.leg2.filled_qty, book.x10_bids, Decimal("0"))
        entry_cost = trade.leg2.entry_price * trade.leg2.filled_qty
        leg2_pnl = revenue - entry_cost
    else:
        # Short Exit: Buy X10 @ Asks
        cost = _calculate_outcome_usd(trade.leg2.filled_qty, book.x10_asks, Decimal("999999"))
        if cost >= Decimal("999999"):
            cost = trade.leg2.entry_price * trade.leg2.filled_qty * Decimal("2")

        entry_revenue = trade.leg2.entry_price * trade.leg2.filled_qty
        leg2_pnl = entry_revenue - cost

    # Subtract fees
    # Note: fees calculation depends on notional value.
    # For simplicity/safety, we can use the Entry Notional (known) or approximate Exit Notional.
    # Let's use trade.total_fees (historical) + estimated exit fees.
    # But wait, this function returns "current realizable PnL NET of exit costs"?
    # The existing `_calculate_realizable_pnl` returns Gross PnL - Historical Fees.
    # The CONSUMER (`_evaluate_exit`) subtracts ESTIMATED EXIT FEES later.
    # So here we should return: Gross PnL - Historical Fees.
    # Wait, checking `_calculate_realizable_pnl`:
    # returns `leg1_pnl + leg2_pnl - total_fees`
    # where total_fees = trade.leg1.fees + trade.leg2.fees (Historical incurred fees).
    # Correct. Exit fees are subtracted in `_evaluate_exit`.

    total_fees = trade.leg1.fees + trade.leg2.fees
    return leg1_pnl + leg2_pnl - total_fees


def _calculate_realized_pnl(self, trade: Trade) -> Decimal:
    """Calculate realized PnL from entry/exit prices."""
    leg1_pnl = trade.leg1.pnl
    leg2_pnl = trade.leg2.pnl

    return leg1_pnl + leg2_pnl
