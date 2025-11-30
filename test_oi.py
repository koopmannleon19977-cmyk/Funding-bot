# test_oi.py
import asyncio
import sys

sys. path.insert(0, "src")

async def test():
    print("OI Tracking - Final Test")
    
    # X10
    from adapters.x10_adapter import X10Adapter
    x10 = X10Adapter()
    await x10.load_market_cache()
    
    for sym in ["BTC-USD", "ETH-USD", "SOL-USD"]:
        oi = await x10.fetch_open_interest(sym)
        print(f"X10 {sym}: ${oi:,.0f}")
    
    print()
    
    # Lighter
    from adapters.lighter_adapter import LighterAdapter
    lighter = LighterAdapter()
    await lighter.initialize()
    
    oi = await lighter.fetch_open_interest("BTC-USD")
    print(f"Lighter BTC-USD: ${oi:,.0f}")
    
    print("\nOI Tracking funktioniert!")

asyncio.run(test())