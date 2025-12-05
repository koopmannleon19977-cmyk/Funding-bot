"""
FORCE CLOSE ALL LIGHTER POSITIONS - WITH AGGRESSIVE PRICING
This script uses very aggressive slippage to force close positions immediately.
"""
import asyncio
import sys
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

import config
config.LIVE_TRADING = True

import sqlite3
import logging
import time
import random

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

async def force_close_all():
    """Force close ALL positions with aggressive pricing"""
    from src.adapters.lighter_adapter import LighterAdapter, HAVE_LIGHTER_SDK, SignerClient
    
    if not HAVE_LIGHTER_SDK:
        logger.error("❌ Lighter SDK not installed!")
        return
    
    lighter = LighterAdapter()
    await lighter.load_market_cache(force=True)
    await lighter.load_funding_rates_and_prices()
    
    logger.info("🔍 Fetching open positions...")
    positions = await lighter.fetch_open_positions()
    
    if not positions:
        logger.info("✅ No open positions!")
        await lighter.aclose()
        return
    
    logger.info(f"🚨 Found {len(positions)} positions:")
    for p in positions:
        symbol = p['symbol']
        size = p['size']
        price = lighter.fetch_mark_price(symbol) or 0
        notional = abs(size) * price
        logger.info(f"   - {symbol}: size={size}, price=${price:.4f}, notional=${notional:.2f}")
    
    signer = await lighter._get_signer()
    
    for position in positions:
        symbol = position['symbol']
        size = float(position['size'])
        
        if abs(size) < 1e-6:
            continue
        
        market_info = lighter.market_info.get(symbol)
        if not market_info:
            logger.error(f"   ❌ {symbol}: No market info!")
            continue
        
        market_id = int(market_info.get('i', 0))
        size_decimals = int(market_info.get('sd', market_info.get('size_decimals', 8)))
        price_decimals = int(market_info.get('pd', market_info.get('price_decimals', 6)))
        
        price = lighter.fetch_mark_price(symbol)
        if not price or price <= 0:
            logger.error(f"   ❌ {symbol}: No price!")
            continue
        
        # Close direction (opposite of position)
        close_side = "SELL" if size > 0 else "BUY"
        close_size = abs(size)
        
        # VERY aggressive slippage (5%) to ensure fill
        slippage = 0.05
        if close_side == "BUY":
            limit_price = price * (1 + slippage)  # Pay more for BUY
        else:
            limit_price = price * (1 - slippage)  # Accept less for SELL
        
        base_scale = 10 ** size_decimals
        quote_scale = 10 ** price_decimals
        
        base_amount = int(close_size * base_scale)
        price_int = int(limit_price * quote_scale)
        
        logger.info(f"   🔻 Closing {symbol}:")
        logger.info(f"      - Size: {close_size} coins")
        logger.info(f"      - Side: {close_side}")
        logger.info(f"      - Price: ${price:.6f} -> ${limit_price:.6f} (5% slippage)")
        
        try:
            client_oid = int(time.time() * 1000) + random.randint(0, 99999)
            
            # Use IOC (time_in_force=0 = IMMEDIATE_OR_CANCEL)
            # Use order_expiry=0 for IOC
            logger.info(f"      Sending IOC order...")
            tx, resp, err = await signer.create_order(
                market_index=market_id,
                client_order_index=client_oid,
                base_amount=base_amount,
                price=price_int,
                is_ask=(close_side == "SELL"),
                order_type=0,  # LIMIT
                time_in_force=0,  # IMMEDIATE_OR_CANCEL
                reduce_only=True,
                trigger_price=0,  # NIL
                order_expiry=0,  # IOC expiry
            )
            
            if err:
                logger.error(f"      ❌ Order error: {err}")
            else:
                tx_hash = getattr(resp, 'tx_hash', 'OK')
                logger.info(f"      ✅ Order sent: {tx_hash}")
            
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"      ❌ Exception: {e}")
            import traceback
            traceback.print_exc()
    
    # Wait and verify
    logger.info("\n⏳ Waiting 5 seconds for fills...")
    await asyncio.sleep(5)
    
    # Force cache clear
    lighter._position_cache = {}
    lighter._position_cache_time = 0
    
    logger.info("🔍 Verifying...")
    remaining = await lighter.fetch_open_positions()
    
    if remaining:
        logger.warning(f"\n⚠️ {len(remaining)} positions STILL OPEN:")
        for p in remaining:
            logger.warning(f"   - {p['symbol']}: size={p['size']}")
        logger.warning("\n🔧 These positions may need manual closing on the Lighter website!")
        logger.warning("   https://app.lighter.xyz/")
    else:
        logger.info("\n✅ ALL POSITIONS CLOSED!")
    
    await lighter.aclose()

def clean_database():
    """Clean database"""
    db_path = os.path.join(project_root, 'data', 'trades.db')
    
    if not os.path.exists(db_path):
        logger.info("✅ No database to clean")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
    count = cursor.fetchone()[0]
    
    if count > 0:
        cursor.execute("""
            UPDATE trades 
            SET status = 'CLOSED', 
                exit_time = datetime('now'),
                close_reason = 'MANUAL_CLEANUP'
            WHERE status = 'OPEN'
        """)
        conn.commit()
        logger.info(f"✅ Closed {count} trades in database")
    else:
        logger.info("✅ Database already clean")
    
    conn.close()

async def main():
    print("=" * 60)
    print("  FORCE CLOSE ALL LIGHTER POSITIONS")
    print("=" * 60)
    print()
    
    await force_close_all()
    
    print()
    print("Cleaning database...")
    clean_database()
    
    print()
    print("=" * 60)
    print("  DONE")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
