"""
Quick check script to see which symbols are available and compare funding rates.
"""
import asyncio
import sys
sys.path.insert(0, '.')

async def main():
    from src.adapters.x10_adapter import X10Adapter
    from src.adapters.lighter_adapter import LighterAdapter
    
    x10 = X10Adapter()
    lit = LighterAdapter()
    
    print("Loading market data...")
    await asyncio.gather(
        x10.load_market_cache(force=True),
        lit.load_market_cache(force=True)
    )
    
    # CRITICAL: Load funding rates explicitly!
    print("Loading funding rates from REST API...")
    await lit.load_funding_rates_and_prices(force=True)
    
    # Check funding cache
    print(f"\n=== LIGHTER FUNDING CACHE STATUS ===")
    print(f"_funding_cache entries: {len(lit._funding_cache)}")
    print(f"funding_cache entries:  {len(lit.funding_cache)}")
    if lit._funding_cache:
        sample = list(lit._funding_cache.items())[:5]
        print(f"Sample rates: {sample}")
    
    common = set(x10.market_info.keys()) & set(lit.market_info.keys())
    
    # Targets from loris.tools
    targets = ['ZORA-USD', 'SHIB-USD', 'SPX-USD', 'CRV-USD', 'SEI-USD', 
               'ADA-USD', 'WLD-USD', 'EIGEN-USD', 'TRUMP-USD']
    
    found = [t for t in targets if t in common]
    missing = [t for t in targets if t not in common]
    
    print(f"\n=== SYMBOL CHECK ===")
    print(f"Total common symbols: {len(common)}")
    print(f"FOUND: {found}")
    print(f"MISSING: {missing}")
    
    # Check funding rates for found symbols
    print(f"\n=== FUNDING RATES (hourly) ===")
    for sym in found[:8]:
        try:
            x10_rate = await x10.fetch_funding_rate(sym)
            lit_rate = await lit.fetch_funding_rate(sym)
            net = abs(float(lit_rate) - float(x10_rate))
            apy = net * 24 * 365 * 100
            direction = "LONG X10, SHORT Lighter" if float(lit_rate) > float(x10_rate) else "SHORT X10, LONG Lighter"
            print(f"{sym}: X10={float(x10_rate)*100:.4f}%/h, Lighter={float(lit_rate)*100:.4f}%/h | Net APY={apy:.1f}% | {direction}")
        except Exception as e:
            print(f"{sym}: Error - {e}")
    
    # Close adapters
    await x10.aclose()
    await lit.aclose()

if __name__ == "__main__":
    asyncio.run(main())

