
import asyncio
import sys
import os
import logging

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

import config
config.LIVE_TRADING = True

from src.adapters.lighter_adapter import LighterAdapter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ONDO_DEBUG")

async def main():
    print("--- DIAGNOSING ONDO ---")
    adapter = LighterAdapter()
    await adapter.initialize()
    
    # 1. Search for ONDO symbol
    target = "ONDO-USD"
    market_info = adapter.market_info.get(target)
    
    if not market_info:
        print(f"❌ '{target}' NOT found in market_info keys!")
        # Try fuzzy search
        print("Searching keys...")
        for k in adapter.market_info.keys():
            if "ONDO" in k:
                print(f"   -> FOUND CLOSE MATCH: {k}")
                target = k
                market_info = adapter.market_info[k]
                break
    
    if not market_info:
        print("❌ Cannot find ONDO market data. Exiting.")
        return

    print(f"✅ Found Market: {target}")
    market_id = market_info.get('i')
    print(f"   ID: {market_id}")
    
    # 2. Fetch Orders RAW
    print("\n--- FETCHING ORDERS ---")
    orders = await adapter.get_open_orders(target)
    print(f"get_open_orders returned {len(orders)} items:")
    for o in orders:
        print(f"   {o}")
        
    if not orders:
        print("⚠️ No orders returned via standard API call.")
        print("Attempting to force manual REST fetch...")
        
        # Manual REST try
        params = {
            "account_index": adapter._resolved_account_index,
            "market_index": market_id,
            "status": 10,
            "limit": 50
        }
        res = await adapter._rest_get("/api/v1/orders", params=params)
        print(f"RAW REST RESPONSE: {res}")
        
    # 3. Force Cancel
    if orders:
        print("\n--- CANCELLING ---")
        for o in orders:
            oid = o['id']
            print(f"Cancelling {oid}...")
            await adapter.cancel_limit_order(oid, target)
    else:
        print("\n⚠️ No orders to cancel found, but user sees them.")

    await adapter.aclose()

if __name__ == "__main__":
    asyncio.run(main())
