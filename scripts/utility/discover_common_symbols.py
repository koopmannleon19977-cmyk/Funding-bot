#!/usr/bin/env python3
"""
Discover common symbols between Lighter and X10 exchanges.

This script fetches available markets from both exchanges and computes
the intersection (common symbols) that can be traded on both platforms.

Usage:
    python scripts/utility/discover_common_symbols.py
"""

import asyncio
import json
from pathlib import Path

import aiohttp


async def fetch_x10_markets() -> set[str]:
    """Fetch all active markets from X10 exchange.

    Returns:
        Set of symbol names (without -USD suffix)
    """
    url = "https://api.starknet.extended.exchange/api/v1/info/markets"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise Exception(f"X10 API returned status {resp.status}")

                data = await resp.json()
                markets = data.get("data", [])

                # Extract active symbols (remove -USD suffix)
                symbols = {m["name"].replace("-USD", "") for m in markets if m.get("active", False)}

                print(f"[OK] X10: {len(symbols)} active markets")
                return symbols

    except Exception as e:
        print(f"[ERROR] Error fetching X10 markets: {e}")
        return set()


async def fetch_lighter_markets() -> set[str]:
    """Fetch all active markets from Lighter exchange.

    Returns:
        Set of symbol names
    """
    url = "https://mainnet.zklighter.elliot.ai/api/v1/perpetual/markets"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    # Fallback: try using the SDK if available
                    print("[WARN]  Lighter REST API failed, trying SDK fallback...")
                    return await _fetch_lighter_markets_sdk()

                data = await resp.json()

                # Handle different response formats
                markets = data.get("data", []) or data.get("markets", []) or data.get("perpetuals", []) or []

                # Extract symbols
                symbols = set()
                for market in markets:
                    # Try different possible field names
                    symbol = (
                        market.get("symbol") or market.get("name") or market.get("pair", "") or market.get("ticker", "")
                    )

                    if not symbol:
                        continue

                    # Normalize symbol (remove -USD, /USD, etc.)
                    symbol = symbol.replace("-USD", "").replace("/USD", "").upper()

                    # Skip inactive markets
                    if market.get("status", "active").lower() != "active":
                        continue

                    symbols.add(symbol)

                if symbols:
                    print(f"[OK] Lighter: {len(symbols)} active markets (REST API)")
                    return symbols

                # Fallback to SDK if REST returns empty
                print("[WARN] Lighter REST API returned empty, trying SDK fallback...")
                return await _fetch_lighter_markets_sdk()

    except Exception as e:
        print(f"[ERROR] Error fetching Lighter markets: {e}")
        print("[INFO] Trying SDK fallback...")
        return await _fetch_lighter_markets_sdk()


async def _fetch_lighter_markets_sdk() -> set[str]:
    """Fetch Lighter markets using SDK as fallback.

    Returns:
        Set of symbol names
    """
    try:
        # Try importing Lighter SDK
        from lighter import LighterClient, LighterConfig

        # Initialize client (no API key needed for public data)
        config = LighterConfig(base_url="https://mainnet.zklighter.elliot.ai")

        async with LighterClient(config=config) as client:
            # Use order_book_details to fetch all market metadata
            result = await client.order_book_details()

            # Extract symbols from result
            perp_details = getattr(result, "order_book_details", []) or []

            symbols = set()
            for d in perp_details:
                symbol = getattr(d, "symbol", None)
                status = getattr(d, "status", "active")

                if symbol and status == "active":
                    symbols.add(symbol)

            print(f"[OK] Lighter: {len(symbols)} active markets (SDK)")
            return symbols

    except ImportError:
        print("[WARN]  Lighter SDK not installed, using hardcoded market list")

        # Fallback: hardcoded list of known Lighter markets
        # This is a subset - update periodically
        known_markets = {
            "0G",
            "1000BONK",
            "1000PEPE",
            "1000SHIB",
            "2Z",
            "4",
            "AAPL",
            "AAVE",
            "ADA",
            "AERO",
            "AMZN",
            "APEX",
            "APT",
            "ARB",
            "ASTER",
            "AVAX",
            "BERA",
            "BNB",
            "BTC",
            "CAKE",
            "CRV",
            "DOGE",
            "EDEN",
            "EIGEN",
            "ENA",
            "ETH",
            "EUR",
            "FARTCOIN",
            "GOAT",
            "GRASS",
            "HYPE",
            "INIT",
            "IP",
            "JUP",
            "KAITO",
            "LDO",
            "LINK",
            "LIT",
            "LTC",
            "MEGA",
            "MELANIA",
            "MKR",
            "MNT",
            "MON",
            "MOODENG",
            "MSFT",
            "NEAR",
            "NFLX",
            "NVDA",
            "ONDO",
            "OP",
            "PENDLE",
            "PENGU",
            "POPCAT",
            "PUMP",
            "RESOLV",
            "S",
            "SEI",
            "SNX",
            "SOL",
            "SPX",
            "SPX500m",
            "STRK",
            "SUI",
            "TAO",
            "TECH100m",
            "TIA",
            "TON",
            "TRUMP",
            "TRX",
            "TSLA",
            "UNI",
            "VIRTUAL",
            "WIF",
            "WLD",
            "WLFI",
            "XAG",
            "XAU",
            "XBR",
            "XMR",
            "XPL",
            "XRP",
            "ZEC",
            "ZORA",
            "ZRO",
        }

        print(f"[OK] Lighter: {len(known_markets)} markets (hardcoded fallback)")
        return known_markets

    except Exception as e:
        print(f"[ERROR] Error fetching Lighter markets via SDK: {e}")

        # Last resort: return hardcoded list
        return {
            "ETH",
            "BTC",
            "SOL",
            "DOGE",
            "PEPE",
            "1000PEPE",
            "1000SHIB",
            "1000BONK",
            "AAPL",
            "AMZN",
            "MSFT",
            "NVDA",
            "TSLA",
            "SPX500m",
            "TECH100m",
            "EUR",
            "XAU",
            "XAG",
        }


async def discover_common_symbols() -> dict:
    """Discover common symbols between both exchanges.

    Returns:
        Dict with discovery results
    """
    print("=" * 60)
    print("Discovering Common Symbols")
    print("=" * 60)

    # Fetch markets from both exchanges
    x10_symbols, lighter_symbols = await asyncio.gather(fetch_x10_markets(), fetch_lighter_markets())

    # Compute intersection (common symbols)
    common_symbols = x10_symbols & lighter_symbols

    # X10-only symbols (not on Lighter)
    x10_only = x10_symbols - lighter_symbols

    # Lighter-only symbols (not on X10)
    lighter_only = lighter_symbols - x10_symbols

    # Prepare results
    results = {
        "x10_count": len(x10_symbols),
        "lighter_count": len(lighter_symbols),
        "common_count": len(common_symbols),
        "x10_only_count": len(x10_only),
        "lighter_only_count": len(lighter_only),
        "common_symbols": sorted(common_symbols),
        "x10_symbols": sorted(x10_symbols),
        "lighter_symbols": sorted(lighter_symbols),
        "x10_only": sorted(x10_only),
        "lighter_only": sorted(lighter_only),
    }

    # Print summary
    print("\n" + "=" * 60)
    print("[RESULTS] DISCOVERY RESULTS")
    print("=" * 60)
    print(f"X10 Markets:       {len(x10_symbols)}")
    print(f"Lighter Markets:   {len(lighter_symbols)}")
    print(f"Common Symbols:    {len(common_symbols)} [COMMON]")
    print(f"X10 Only:          {len(x10_only)}")
    print(f"Lighter Only:      {len(lighter_only)}")

    if common_symbols:
        print(f"\n[COMMON] COMMON SYMBOLS ({len(common_symbols)}):")
        print("-" * 60)
        # Convert to sorted list for printing
        sorted_symbols = sorted(common_symbols)
        # Print in columns of 8
        for i in range(0, len(sorted_symbols), 8):
            row = sorted_symbols[i : i + 8]
            print(", ".join(f"{s:<10}" for s in row))

    if lighter_only and len(lighter_only) <= 20:
        print(f"\n[WARN]  LIGHTER-ONLY SYMBOLS ({len(lighter_only)}):")
        print("-" * 60)
        print(", ".join(lighter_only))

    return results


def save_common_symbols_config(results: dict, output_path: str = "config/common_symbols.json"):
    """Save common symbols to config file.

    Args:
        results: Discovery results dict
        output_path: Path to save config file
    """
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Create config structure
    config = {
        "metadata": {
            "x10_markets": results["x10_count"],
            "lighter_markets": results["lighter_count"],
            "common_symbols": results["common_count"],
            "discovered_at": None,  # Will be set to current time
        },
        "common_symbols": results["common_symbols"],
        "x10_only": results["x10_only"],
        "lighter_only": results["lighter_only"],
    }

    # Add timestamp
    from datetime import UTC, datetime

    config["metadata"]["discovered_at"] = datetime.now(UTC).isoformat() + "Z"

    # Write to file
    with open(output_file, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\n[SAVE] Saved common symbols config to: {output_path}")


async def main():
    """Main entry point."""
    # Discover common symbols
    results = await discover_common_symbols()

    # Save to config
    save_common_symbols_config(results)

    # Print next steps
    print("\n" + "=" * 60)
    print("[NEXT] NEXT STEPS")
    print("=" * 60)
    print("1. Review the common symbols in config/common_symbols.json")
    print("2. Update config.yaml to use common_symbols whitelist:")
    print("   trading:")
    print("     symbols: load_from_file  # or use common_symbols.json")
    print("3. Restart the bot to use filtered symbol list")

    if results["common_symbols"]:
        print(f"\n[OK] SUCCESS: {len(results['common_symbols'])} common symbols found!")
        print("   The bot will now only try to fetch funding history for")
        print("   symbols that exist on BOTH exchanges.")
    else:
        print("\n[WARN]  WARNING: No common symbols found!")
        print("   This might indicate a problem with market discovery.")


if __name__ == "__main__":
    asyncio.run(main())
