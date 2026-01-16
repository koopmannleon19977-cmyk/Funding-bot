#!/usr/bin/env python3
"""
Debug script to inspect X10 SDK PositionModel attributes.
Specifically looking for liquidation_price field mapping.

Usage:
    python scripts/utility/debug_x10_liquidation.py
"""

import asyncio
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from funding_bot.config.settings import get_settings


async def debug_x10_positions():
    """Inspect X10 position objects for liquidation_price attribute."""

    print("=" * 60)
    print("X10 SDK Position Attribute Debug")
    print(f"Time: {datetime.now().isoformat()}")
    print("=" * 60)

    # Try to import SDK
    try:
        from x10.perpetual.accounts import StarkPerpetualAccount
        from x10.perpetual.trading_client import PerpetualTradingClient
        print("\n✅ X10 SDK imported successfully")
    except ImportError as e:
        print(f"\n❌ X10 SDK import failed: {e}")
        print("Install with: pip install x10-python-trading-starknet")
        return

    # Load settings
    settings = get_settings()
    x10_cfg = settings.x10

    if not x10_cfg.api_key:
        print("\n❌ X10 API credentials not configured in settings")
        return

    print(f"\n📡 Connecting to X10...")
    print(f"   Environment: {'TESTNET' if x10_cfg.testnet else 'MAINNET'}")

    try:
        # Create account and client
        stark_account = StarkPerpetualAccount(
            vault=x10_cfg.vault,
            private_key=x10_cfg.private_key,
            public_key=x10_cfg.public_key,
            api_key=x10_cfg.api_key,
        )

        trading_client = await PerpetualTradingClient.create(
            stark_account,
            env="testnet" if x10_cfg.testnet else "mainnet"
        )

        print("✅ Connected to X10")

        # Fetch positions
        print("\n📊 Fetching positions...")
        response = await trading_client.account.get_positions()
        positions_data = getattr(response, "data", response) or []

        if not positions_data:
            print("⚠️  No open positions found")
            print("\nTo test, open a position on X10 first.")
            return

        print(f"✅ Found {len(positions_data)} position(s)\n")

        # Analyze each position
        results = []
        for i, p in enumerate(positions_data):
            print(f"\n{'='*50}")
            print(f"Position {i+1}: {getattr(p, 'market', 'UNKNOWN')}")
            print(f"{'='*50}")

            # Get all public attributes
            attrs = [a for a in dir(p) if not a.startswith('_')]

            result = {
                "index": i + 1,
                "market": getattr(p, "market", None),
                "all_attributes": attrs,
                "attribute_values": {},
                "liquidation_analysis": {}
            }

            # Key attributes we care about
            key_attrs = [
                "market", "side", "size", "leverage",
                "open_price", "mark_price", "liquidation_price",
                "unrealised_pnl", "realised_pnl",
                "liq_price", "liquidation", "liquidationPrice",  # Alternative names
            ]

            print("\n📋 Key Attributes:")
            for attr in key_attrs:
                value = getattr(p, attr, "<<NOT_FOUND>>")
                if value != "<<NOT_FOUND>>":
                    result["attribute_values"][attr] = str(value) if value is not None else None

                    # Highlight liquidation-related
                    if "liq" in attr.lower():
                        marker = "🎯"
                    else:
                        marker = "  "

                    print(f"   {marker} {attr}: {value} (type: {type(value).__name__})")

            # Specific liquidation_price analysis
            print("\n🔍 Liquidation Price Analysis:")

            liq_price = getattr(p, "liquidation_price", "ATTR_MISSING")
            result["liquidation_analysis"]["raw_value"] = str(liq_price)
            result["liquidation_analysis"]["type"] = type(liq_price).__name__

            if liq_price == "ATTR_MISSING":
                print("   ❌ Attribute 'liquidation_price' does NOT exist on object")
                result["liquidation_analysis"]["status"] = "ATTRIBUTE_MISSING"
            elif liq_price is None:
                print("   ⚠️  Attribute exists but value is None")
                result["liquidation_analysis"]["status"] = "VALUE_IS_NONE"
            elif liq_price == 0 or liq_price == Decimal("0"):
                print("   ⚠️  Attribute exists but value is 0")
                result["liquidation_analysis"]["status"] = "VALUE_IS_ZERO"
            else:
                print(f"   ✅ Attribute exists with value: {liq_price}")
                result["liquidation_analysis"]["status"] = "OK"

            # Check for alternative attribute names
            print("\n🔎 Checking alternative attribute names:")
            alternatives = ["liq_price", "liquidation", "liquidationPrice", "liqPrice"]
            for alt in alternatives:
                alt_val = getattr(p, alt, "NOT_FOUND")
                if alt_val != "NOT_FOUND":
                    print(f"   Found '{alt}': {alt_val}")
                    result["liquidation_analysis"][f"alt_{alt}"] = str(alt_val)

            # Raw __dict__ inspection
            if hasattr(p, "__dict__"):
                print("\n📦 Raw __dict__ keys:")
                dict_keys = list(p.__dict__.keys())
                print(f"   {dict_keys}")
                result["raw_dict_keys"] = dict_keys

            # Pydantic model fields (if applicable)
            if hasattr(p, "model_fields"):
                print("\n📐 Pydantic model_fields:")
                fields = list(p.model_fields.keys())
                print(f"   {fields}")
                result["pydantic_fields"] = fields

            results.append(result)

        # Summary
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)

        for r in results:
            market = r["market"]
            status = r["liquidation_analysis"]["status"]
            print(f"   {market}: {status}")

        # Save to file
        output_file = Path(__file__).parent.parent.parent / "logs" / "x10_liquidation_debug.json"
        output_file.parent.mkdir(exist_ok=True)

        output_data = {
            "timestamp": datetime.now().isoformat(),
            "positions": results
        }

        with open(output_file, "w") as f:
            json.dump(output_data, f, indent=2, default=str)

        print(f"\n💾 Results saved to: {output_file}")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        if 'trading_client' in locals():
            try:
                await trading_client.close()
            except:
                pass


if __name__ == "__main__":
    asyncio.run(debug_x10_positions())
