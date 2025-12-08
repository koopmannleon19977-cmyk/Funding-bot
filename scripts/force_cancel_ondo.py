
import asyncio
import sys
import os
import logging
import time

# Setup paths
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

import config
config.LIVE_TRADING = True

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FORCE_CANCEL")

async def main():
    print(f"Python Executable: {sys.executable}")
    
    try:
        from src.adapters.lighter_adapter import LighterAdapter, HAVE_LIGHTER_SDK
    except ImportError as e:
        print(f"CRITICAL IMPORT ERROR: {e}")
        return

    if not HAVE_LIGHTER_SDK:
        print("❌ Lighter SDK not detected by Adapter! check pip install.")
        return

    print("✅ Adapter imported. Initializing...")
    adapter = LighterAdapter()
    await adapter.initialize()

    # Wait for markets
    if not adapter.market_info:
        print("⚠️ Market Info empty. Waiting 2s...")
        await asyncio.sleep(2)
    
    print(f"Markets loaded: {len(adapter.market_info)}")
    
    # Search for ONDO
    candidates = [k for k in adapter.market_info.keys() if "ONDO" in k]
    print(f"Found ONDO candidates: {candidates}")
    
    if not candidates:
        print("❌ ONDO not found in markets.")
        await adapter.aclose()
        return

    for sym in candidates:
        print(f"\nProcessing {sym}...")
        
        # 1. Fetch Orders
        orders = await adapter.get_open_orders(sym)
        print(f"   Open Orders: {len(orders)}")
        
        for o in orders:
            print(f"   -> Found Order: {o}")
            oid = o.get('id')
            if oid:
                print(f"      Attempting Cancel {oid}...")
                success = await adapter.cancel_limit_order(oid, sym)
                print(f"      Result: {success}")
        
        # 2. Force Blind Cancel just in case
        print(f"   -> Sending Force Cancel All for {sym}...")
        await adapter.cancel_all_orders(sym)

    print("\nDone.")
    await adapter.aclose()

if __name__ == "__main__":
    asyncio.run(main())
