#!/usr/bin/env python3
"""
Debug script to list common symbols and check for specific tokens.
Run this with: python check_symbols.py
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from funding_bot.adapters.exchanges.lighter.adapter import LighterAdapter
from funding_bot.adapters.exchanges.x10.adapter import X10Adapter
from funding_bot.config.settings import Settings


async def main():
    """Load and compare markets from both exchanges."""
    settings = Settings()

    # Initialize adapters
    lighter = LighterAdapter(settings)
    x10 = X10Adapter(settings)

    # Initialize adapters (required for Lighter)
    print("Initializing adapters...")
    await lighter.initialize()
    await x10.initialize()

    # Load markets (no API keys needed for market list)
    print("Loading Lighter markets...")
    lighter_markets = await lighter.load_markets()
    print(f"Lighter: {len(lighter_markets)} markets")

    print("\nLoading X10 markets...")
    x10_markets = await x10.load_markets()
    print(f"X10: {len(x10_markets)} markets")

    # Get symbol sets
    lighter_symbols = set(lighter_markets.keys())
    x10_symbols = set(x10_markets.keys())

    # Normalize X10 symbols (remove -USD suffix)
    x10_base_symbols = {s.replace("-USD", "") for s in x10_symbols}

    # Find common symbols
    common = lighter_symbols & x10_base_symbols

    print(f"\nCommon symbols: {len(common)}")

    # Check for specific tokens from lors.tools
    target_tokens = ["XMR", "TRUMP", "ZEC", "JUP", "TON", "ZORA", "BTC", "ETH", "BERA", "IP"]

    print("\n=== Token Availability Check ===")
    for token in target_tokens:
        lighter_has = token in lighter_symbols
        x10_has = token in x10_base_symbols
        common_has = token in common
        print(f"{token}: Lighter={lighter_has}, X10={x10_has}, Common={common_has}")

    # Show all common symbols
    print(f"\n=== All {len(common)} Common Symbols ===")
    for symbol in sorted(common):
        lighter_info = lighter_markets.get(symbol)
        x10_symbol = f"{symbol}-USD"
        x10_info = x10_markets.get(x10_symbol)

        lighter_lev = getattr(lighter_info, "lighter_max_leverage", "N/A") if lighter_info else "N/A"
        x10_lev = getattr(x10_info, "x10_max_leverage", "N/A") if x10_info else "N/A"

        print(f"  {symbol}: Lighter_lev={lighter_lev}, X10_lev={x10_lev}")

    # Show Lighter-only symbols (high APY candidates)
    lighter_only = lighter_symbols - x10_base_symbols
    print(f"\n=== Lighter-Only Symbols ({len(lighter_only)}) ===")
    print("These tokens are on Lighter but NOT on X10:")
    for symbol in sorted(lighter_only)[:20]:  # Show first 20
        print(f"  {symbol}")

    # Show X10-only symbols
    x10_only = x10_base_symbols - lighter_symbols
    print(f"\n=== X10-Only Symbols ({len(x10_only)}) ===")
    print("These tokens are on X10 but NOT on Lighter:")
    for symbol in sorted(x10_only)[:20]:  # Show first 20
        print(f"  {symbol}")


if __name__ == "__main__":
    asyncio.run(main())
