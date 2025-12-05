"""Check current positions on Lighter"""
import asyncio
import sys
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

import config
config.LIVE_TRADING = True

async def main():
    from src.adapters.lighter_adapter import LighterAdapter
    
    lighter = LighterAdapter()
    await lighter.load_market_cache(force=True)
    
    print("Fetching Lighter positions...")
    positions = await lighter.fetch_open_positions()
    
    if not positions:
        print("✅ No open positions on Lighter")
    else:
        print(f"🚨 Found {len(positions)} positions:")
        for p in positions:
            print(f"   - {p['symbol']}: size={p['size']}")
    
    await lighter.aclose()

if __name__ == "__main__":
    asyncio.run(main())
