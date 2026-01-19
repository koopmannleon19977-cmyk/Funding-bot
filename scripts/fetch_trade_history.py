#!/usr/bin/env python3
"""
Fetch trade history from Lighter and X10 for a specific symbol.
Usage: python scripts/fetch_trade_history.py EDEN
"""

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal

# Add src to path
sys.path.insert(0, "src")

# Load .env file from project root
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)
print(f"Loading .env from: {env_path}")

from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
from funding_bot.adapters.exchanges.x10.adapter import X10Adapter
from funding_bot.config import Settings


async def fetch_lighter_trades(adapter: LighterAdapter, symbol: str, days_back: int = 7):
    """Fetch trades from Lighter API."""
    print(f"\n{'=' * 60}")
    print(f"LIGHTER TRADES for {symbol}")
    print(f"{'=' * 60}")

    try:
        market_id = adapter._get_market_index(symbol)
        if market_id < 0:
            print(f"Market not found: {symbol}")
            return []

        # Use the SDK to fetch trades
        trades_resp = await adapter._sdk_call_with_retry(
            lambda: adapter._order_api.trades(
                sort_by="timestamp",
                limit=100,
                market_id=market_id,
                account_index=adapter._account_index,
                sort_dir="desc",
                auth=adapter._auth_token,
            )
        )

        trades = getattr(trades_resp, "trades", []) or []

        if not trades:
            print("No trades found")
            return []

        print(f"\nFound {len(trades)} trades:\n")
        print(f"{'Timestamp':<25} {'Side':<6} {'Qty':>12} {'Price':>10} {'Notional':>12} {'Fee':>10} {'Type':<8}")
        print("-" * 95)

        total_buy_qty = Decimal("0")
        total_sell_qty = Decimal("0")
        total_buy_notional = Decimal("0")
        total_sell_notional = Decimal("0")
        total_fee = Decimal("0")

        for t in trades:
            ts = getattr(t, "timestamp", "")
            is_ask = getattr(t, "is_ask", False)
            side = "SELL" if is_ask else "BUY"
            qty = Decimal(str(getattr(t, "base_amount", 0) or 0))
            price = Decimal(str(getattr(t, "price", 0) or 0))
            notional = qty * price
            fee = Decimal(str(getattr(t, "fee", 0) or 0))
            trade_type = "Taker" if getattr(t, "is_taker", False) else "Maker"

            # Convert timestamp
            if isinstance(ts, (int, float)):
                ts_str = datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")
            else:
                ts_str = str(ts)[:25]

            print(f"{ts_str:<25} {side:<6} {qty:>12.4f} {price:>10.6f} ${notional:>10.2f} ${fee:>9.4f} {trade_type:<8}")

            if side == "BUY":
                total_buy_qty += qty
                total_buy_notional += notional
            else:
                total_sell_qty += qty
                total_sell_notional += notional
            total_fee += fee

        print("-" * 95)
        print(f"{'TOTALS':<25} {'BUY':<6} {total_buy_qty:>12.4f} {'':>10} ${total_buy_notional:>10.2f}")
        print(f"{'':25} {'SELL':<6} {total_sell_qty:>12.4f} {'':>10} ${total_sell_notional:>10.2f}")
        print(f"{'':25} {'FEE':<6} {'':>12} {'':>10} ${total_fee:>10.4f}")

        return trades

    except Exception as e:
        print(f"Error fetching Lighter trades: {e}")
        import traceback

        traceback.print_exc()
        return []


async def fetch_x10_trades(adapter: X10Adapter, symbol: str, days_back: int = 7):
    """Fetch trades from X10 API."""
    print(f"\n{'=' * 60}")
    print(f"X10 TRADES for {symbol}")
    print(f"{'=' * 60}")

    try:
        market_name = f"{symbol}-USD"

        # Use the SDK to fetch trades
        trades_resp = await adapter._sdk_call_with_retry(
            lambda: adapter._trading_client.account.get_trades(
                market_names=[market_name],
                limit=200,
            ),
            operation_name="get_trades",
        )

        trades = getattr(trades_resp, "data", None) or []

        if not trades:
            print("No trades found")
            return []

        print(f"\nFound {len(trades)} trades:\n")
        print(f"{'Timestamp':<25} {'Side':<6} {'Qty':>12} {'Price':>10} {'Notional':>12} {'Fee':>10} {'OrderID':<20}")
        print("-" * 105)

        total_buy_qty = Decimal("0")
        total_sell_qty = Decimal("0")
        total_buy_notional = Decimal("0")
        total_sell_notional = Decimal("0")
        total_fee = Decimal("0")

        for t in trades:
            ts = getattr(t, "created_at", "") or getattr(t, "timestamp", "")
            side = str(getattr(t, "side", "")).upper()
            qty = Decimal(str(getattr(t, "qty", 0) or getattr(t, "quantity", 0) or 0))
            price = Decimal(str(getattr(t, "price", 0) or 0))
            notional = qty * price
            fee = Decimal(str(getattr(t, "fee", 0) or 0))
            order_id = str(getattr(t, "order_id", ""))[:20]

            # Convert timestamp
            if isinstance(ts, (int, float)):
                ts_str = datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")
            else:
                ts_str = str(ts)[:25]

            print(f"{ts_str:<25} {side:<6} {qty:>12.4f} {price:>10.6f} ${notional:>10.2f} ${fee:>9.4f} {order_id:<20}")

            if "BUY" in side:
                total_buy_qty += qty
                total_buy_notional += notional
            else:
                total_sell_qty += qty
                total_sell_notional += notional
            total_fee += fee

        print("-" * 105)
        print(f"{'TOTALS':<25} {'BUY':<6} {total_buy_qty:>12.4f} {'':>10} ${total_buy_notional:>10.2f}")
        print(f"{'':25} {'SELL':<6} {total_sell_qty:>12.4f} {'':>10} ${total_sell_notional:>10.2f}")
        print(f"{'':25} {'FEE':<6} {'':>12} {'':>10} ${total_fee:>10.4f}")

        return trades

    except Exception as e:
        print(f"Error fetching X10 trades: {e}")
        import traceback

        traceback.print_exc()
        return []


async def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "EDEN"
    days_back = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    print(f"\nFetching trade history for {symbol} (last {days_back} days)")
    print("=" * 60)

    # Load settings
    settings = Settings()

    # Debug: Check if credentials are loaded
    print(f"\nLighter Private Key: {'SET' if settings.lighter.private_key else 'NOT SET'}")
    print(f"Lighter Account Index: {settings.lighter.account_index}")
    print(f"X10 Private Key: {'SET' if settings.x10.private_key else 'NOT SET'}")
    print(f"X10 Vault ID: {settings.x10.vault_id}")

    # Initialize adapters
    lighter = LighterAdapter(settings)
    x10 = X10Adapter(settings)

    try:
        # Initialize both adapters
        print("\nInitializing Lighter adapter...")
        await lighter.initialize()
        print(f"Lighter auth token: {'SET' if lighter._auth_token else 'NOT SET'}")

        print("Initializing X10 adapter...")
        await x10.initialize()
        print(f"X10 trading client: {'SET' if x10._trading_client else 'NOT SET'}")

        # Fetch trades from both
        await fetch_lighter_trades(lighter, symbol, days_back)
        await fetch_x10_trades(x10, symbol, days_back)

    finally:
        # Cleanup
        await lighter.close()
        await x10.close()


if __name__ == "__main__":
    asyncio.run(main())
