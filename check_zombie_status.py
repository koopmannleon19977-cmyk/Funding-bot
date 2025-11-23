# check_zombie_status.py
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
from src.adapters.x10_adapter import X10Adapter
from src.adapters.lighter_adapter import LighterAdapter
import aiosqlite

async def check_for_zombies():
    """Manual zombie check"""
    x10 = X10Adapter()
    lighter = LighterAdapter()
    
    # Load market data
    await x10.load_market_cache(force=True)
    await lighter.load_market_cache(force=True)
    
    # Get positions
    x10_pos = await x10.fetch_open_positions()
    lighter_pos = await lighter.fetch_open_positions()
    
    print("\n📊 CURRENT STATE:")
    print(f"X10 Positions: {len(x10_pos or [])}")
    for p in (x10_pos or []):
        print(f"  - {p['symbol']}: {p['size']:.6f}")
    
    print(f"\nLighter Positions: {len(lighter_pos or [])}")
    for p in (lighter_pos or []):
        print(f"  - {p['symbol']}: {p['size']:.6f}")
    
    # Get DB trades
    async with aiosqlite.connect(config.DB_FILE) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT symbol, status FROM trades WHERE status = 'OPEN'") as cursor:
            db_trades = await cursor.fetchall()
    
    print(f"\nDB Open Trades: {len(db_trades)}")
    for t in db_trades:
        print(f"  - {t['symbol']}")
    
    # Detect zombies
    exchange_syms = set()
    for p in (x10_pos or []) + (lighter_pos or []):
        if abs(p.get('size', 0)) > 1e-8:
            exchange_syms.add(p['symbol'])
    
    db_syms = {t['symbol'] for t in db_trades}
    
    zombies = exchange_syms - db_syms
    db_zombies = db_syms - exchange_syms
    
    if zombies:
        print(f"\n🧟 EXCHANGE ZOMBIES (on exchange, not in DB): {zombies}")
    if db_zombies:
        print(f"\n🗄️ DB ZOMBIES (in DB, not on exchange): {db_zombies}")
    
    if not zombies and not db_zombies:
        print("\n✅ NO ZOMBIES DETECTED")
    
    await x10.aclose()
    await lighter.aclose()

asyncio.run(check_for_zombies())