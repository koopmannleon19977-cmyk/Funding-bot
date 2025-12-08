#!/usr/bin/env python3
"""Check actual positions on both exchanges"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter

async def main():
    print("🔍 Checking exchange positions...")
    
    x10 = X10Adapter()
    lighter = LighterAdapter()
    
    print("\n" + "="*60)
    print("📊 X10 POSITIONS")
    print("="*60)
    
    try:
        x10_positions = await x10.fetch_open_positions()
        if x10_positions:
            for p in x10_positions:
                symbol = p.get('symbol')
                size = p.get('size', 0)
                side = p.get('side')
                pnl = p.get('unrealised_pnl', 0)
                print(f"   {symbol}: {side} {size} (PnL: ${pnl})")
        else:
            print("   ✅ No open positions")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print("\n" + "="*60)
    print("📊 LIGHTER POSITIONS")
    print("="*60)
    
    try:
        lighter_positions = await lighter.fetch_open_positions()
        if lighter_positions:
            for p in lighter_positions:
                symbol = p.get('symbol')
                size = p.get('size', 0)
                side = p.get('side')
                pnl = p.get('unrealised_pnl', 0)
                print(f"   {symbol}: {side} {size} (PnL: ${pnl})")
        else:
            print("   ✅ No open positions")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    # Compare
    print("\n" + "="*60)
    print("🔍 DESYNC CHECK")
    print("="*60)
    
    x10_syms = set(p.get('symbol') for p in (x10_positions or []))
    lighter_syms = set(p.get('symbol') for p in (lighter_positions or []))
    
    only_x10 = x10_syms - lighter_syms
    only_lighter = lighter_syms - x10_syms
    both = x10_syms & lighter_syms
    
    if only_x10:
        print(f"⚠️  ONLY on X10 (unhedged!): {only_x10}")
    if only_lighter:
        print(f"⚠️  ONLY on Lighter (unhedged!): {only_lighter}")
    if both:
        print(f"✅ Hedged on both: {both}")
    if not x10_syms and not lighter_syms:
        print("✅ All clear - no positions on either exchange!")
    
    await x10.aclose()
    await lighter.aclose()

if __name__ == "__main__":
    asyncio.run(main())

