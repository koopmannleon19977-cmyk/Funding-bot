# test_x10_funding.py
import asyncio
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG

async def test():
    client = PerpetualTradingClient(MAINNET_CONFIG)
    resp = await client.markets_info.get_markets()
    
    if resp and resp.data:
        m = resp.data[0]  # Erstes Market
        print(f"Market: {m.name}")
        print(f"Market attrs: {dir(m)}")
        
        stats = getattr(m, 'market_stats', None)
        if stats:
            print(f"Stats attrs: {dir(stats)}")
            print(f"Stats dict: {vars(stats) if hasattr(stats, '__dict__') else stats}")

asyncio.run(test())