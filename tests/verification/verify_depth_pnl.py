import asyncio
import sys
from decimal import Decimal
from unittest.mock import MagicMock

# Adjust path to include src
sys.path.append("c:/Users/koopm/funding-bot/src")

from funding_bot.domain.models import Exchange, Side, Trade, TradeLeg, TradeStatus
from funding_bot.services.market_data import OrderbookDepthSnapshot
from funding_bot.services.positions import PositionManager


async def verify_depth_pnl():
    print("--- Verifying Depth-Aware PnL Calculation ---")

    # 1. Setup Mock Manager
    settings = MagicMock()
    # Mock fee schedule
    mock_market_data = MagicMock()
    async def mock_get_fees(symbol):
        return {
            "lighter_taker": Decimal("0.0005"),
            "x10_taker": Decimal("0.0005")
        }
    mock_market_data.get_fee_schedule = mock_get_fees

    manager = PositionManager(
        settings=settings,
        lighter=MagicMock(),
        x10=MagicMock(),
        store=MagicMock(),
        event_bus=MagicMock(),
        market_data=mock_market_data
    )

    # 2. Setup Trade (Long Lighter, Short X10)
    # Entry: Lighter @ 100, X10 @ 100. Size: 100.
    trade = Trade(
        trade_id="test_trade",
        symbol="TEST",
        status=TradeStatus.OPEN,
        target_qty=Decimal("100"),
        target_notional_usd=Decimal("10000"),
        leg1=TradeLeg(
            exchange=Exchange.LIGHTER,
            side=Side.BUY,
            entry_price=Decimal("100"),
            qty=Decimal("100"),
            filled_qty=Decimal("100"),
            fees=Decimal("0")
        ),
        leg2=TradeLeg(
            exchange=Exchange.X10,
            side=Side.SELL,
            entry_price=Decimal("100"),
            qty=Decimal("100"),
            filled_qty=Decimal("100"),
            fees=Decimal("0")
        )
    )

    # 3. Scenario: Shallow Book (Price Crash)
    # Market Price seems fine at L1 (101), but depth crashes immediately.
    # Lighter (we are Long, so we SELL at Bid):
    #   - 10 qty @ 101 (Best Bid)
    #   - 90 qty @ 90  (Depth crash)
    #   Weighted Avg Exit = (10*101 + 90*90) / 100 = (1010 + 8100)/100 = 91.1

    # X10 (we are Short, so we BUY at Ask):
    #   - 10 qty @ 99 (Best Ask)
    #   - 90 qty @ 99 (Stable)
    #   Avg Exit = 99

    # Expected PnL:
    # Leg 1 (Long, Exit 91.1): (91.1 - 100) * 100 = -890
    # Leg 2 (Short, Exit 99):  (100 - 99) * 100  = +100
    # Gross PnL = -790

    # Legacy L1 PnL would see:
    # Leg 1 (Exit 101): (101 - 100) * 100 = +100
    # Leg 2 (Exit 99):  (100 - 99) * 100  = +100
    # Gross PnL = +200  <-- MASSIVE DISCREPANCY

    depth_snapshot = OrderbookDepthSnapshot(
        symbol="TEST",
        lighter_bids=[(Decimal("101"), Decimal("10")), (Decimal("90"), Decimal("90"))],
        lighter_asks=[(Decimal("102"), Decimal("100"))],
        x10_bids=[(Decimal("98"), Decimal("100"))],
        x10_asks=[(Decimal("99"), Decimal("100"))],
        lighter_updated=MagicMock(),
        x10_updated=MagicMock()
    )

    # Temporary monkey-patch to test the NEW logic (which doesn't exist yet, so we verify fail first via AttributeError or similar if we tried to call it,
    # but here I will just implement the calculation function locally to verify the LOGIC itself before putting it in class)

    # ... actually, let's wait to run this until I've added the method to the class.
    # checking existence of method:
    if not hasattr(manager, "_calculate_realizable_pnl_depth"):
        print("FAIL: _calculate_realizable_pnl_depth not implemented yet.")
        return

    # Call the new method
    pnl = await manager._calculate_realizable_pnl_depth(trade, depth_snapshot)

    print(f"Calculated Depth PnL: {pnl}")

    # Fees: roughly 0.001 * 100 * 100 = 10 (approx)
    # Adjusted for fees: -790 - fees.

    # Let's perform a rough assertion
    if pnl > 0:
         print("FAIL: PnL is positive (Legacy L1 behavior?)")
    elif pnl < -700:
         print("PASS: PnL reflects depth crash.")
    else:
         print(f"WARN: PnL {pnl} unexpected.")

if __name__ == "__main__":
    asyncio.run(verify_depth_pnl())
