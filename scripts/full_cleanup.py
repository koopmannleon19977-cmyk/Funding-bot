"""
CLEANUP SCRIPT: Force-close all Lighter positions and clean database
This script uses IOC (Immediate-Or-Cancel) orders to force close positions.
"""
import asyncio
import sys
import os

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

import config
config.LIVE_TRADING = True  # Enable live trading for cleanup

import sqlite3
import logging
from decimal import Decimal

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

async def force_close_lighter_positions():
    """Force close ALL positions on Lighter using IOC orders"""
    from src.adapters.lighter_adapter import LighterAdapter, HAVE_LIGHTER_SDK
    
    if not HAVE_LIGHTER_SDK:
        logger.error("❌ Lighter SDK not installed!")
        return False
    
    lighter = LighterAdapter()
    await lighter.load_market_cache(force=True)
    
    logger.info("🔍 Fetching open positions from Lighter...")
    positions = await lighter.fetch_open_positions()
    
    if not positions:
        logger.info("✅ No open positions on Lighter")
        return True
    
    logger.info(f"🚨 Found {len(positions)} positions to close:")
    for p in positions:
        logger.info(f"   - {p['symbol']}: size={p['size']}")
    
    # Import SignerClient for IOC orders
    from lighter.lighter_client import SignerClient
    from lighter.api.order_api import OrderApi
    import time
    import random
    
    signer = await lighter._get_signer()
    closed_count = 0
    
    for position in positions:
        symbol = position['symbol']
        size = float(position['size'])
        
        if abs(size) < 1e-8:
            logger.info(f"   ⏭️ {symbol}: Size too small, skipping")
            continue
        
        # Get market info
        market_info = lighter.market_info.get(symbol)
        if not market_info:
            logger.error(f"   ❌ {symbol}: No market info!")
            continue
        
        market_id = int(market_info.get('i', 0))
        if market_id <= 0:
            logger.error(f"   ❌ {symbol}: Invalid market_id!")
            continue
        
        # Get price
        price = lighter.fetch_mark_price(symbol)
        if not price or price <= 0:
            logger.error(f"   ❌ {symbol}: No price available!")
            continue
        
        # Determine close side (opposite of position)
        close_side = "SELL" if size > 0 else "BUY"
        close_size_coins = abs(size)
        
        # Calculate base amount and price
        size_decimals = int(market_info.get('sd', market_info.get('size_decimals', 8)))
        price_decimals = int(market_info.get('pd', market_info.get('price_decimals', 6)))
        
        base_scale = 10 ** size_decimals
        quote_scale = 10 ** price_decimals
        
        # Wider slippage for IOC
        slippage = 0.02  # 2%
        if close_side == "BUY":
            limit_price = price * (1 + slippage)
        else:
            limit_price = price * (1 - slippage)
        
        base_amount = int(close_size_coins * base_scale)
        price_int = int(limit_price * quote_scale)
        
        logger.info(f"   🔻 Closing {symbol}: {close_size_coins:.4f} coins @ ${limit_price:.6f} ({close_side})")
        
        try:
            client_oid = int(time.time() * 1000) + random.randint(0, 99999)
            
            # Use IOC (Immediate-Or-Cancel) for immediate execution
            tx, resp, err = await signer.create_order(
                market_index=market_id,
                client_order_index=client_oid,
                base_amount=base_amount,
                price=price_int,
                is_ask=(close_side == "SELL"),
                order_type=SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IOC,  # CRITICAL: IOC for immediate fill
                reduce_only=True,
                trigger_price=SignerClient.NIL_TRIGGER_PRICE,
                order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY,
            )
            
            if err:
                logger.error(f"   ❌ {symbol}: Order error: {err}")
            else:
                tx_hash = getattr(resp, 'tx_hash', 'OK')
                logger.info(f"   ✅ {symbol}: Order submitted (tx={tx_hash})")
                closed_count += 1
                
            # Small delay between orders
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"   ❌ {symbol}: Exception: {e}")
    
    # Wait a bit for orders to process
    logger.info("⏳ Waiting 3 seconds for orders to process...")
    await asyncio.sleep(3)
    
    # Verify positions are closed
    logger.info("🔍 Verifying positions are closed...")
    remaining = await lighter.fetch_open_positions()
    
    if remaining:
        logger.warning(f"⚠️ {len(remaining)} positions still open:")
        for p in remaining:
            logger.warning(f"   - {p['symbol']}: size={p['size']}")
    else:
        logger.info("✅ All Lighter positions closed!")
    
    await lighter.aclose()
    return len(remaining) == 0

def clean_database():
    """Clean all OPEN trades from database"""
    db_path = os.path.join(project_root, 'data', 'trades.db')
    
    if not os.path.exists(db_path):
        logger.info("✅ Database doesn't exist - nothing to clean")
        return True
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check current open trades
        cursor.execute("SELECT id, symbol, status FROM trades WHERE status = 'OPEN'")
        open_trades = cursor.fetchall()
        
        if not open_trades:
            logger.info("✅ No OPEN trades in database")
            conn.close()
            return True
        
        logger.info(f"🧹 Found {len(open_trades)} OPEN trades in database:")
        for trade in open_trades:
            logger.info(f"   - ID={trade[0]}, Symbol={trade[1]}")
        
        # Close all open trades
        cursor.execute("""
            UPDATE trades 
            SET status = 'CLOSED', 
                exit_time = datetime('now'),
                close_reason = 'MANUAL_CLEANUP'
            WHERE status = 'OPEN'
        """)
        
        affected = cursor.rowcount
        conn.commit()
        
        logger.info(f"✅ Closed {affected} trades in database")
        
        # Verify
        cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
        remaining = cursor.fetchone()[0]
        
        if remaining > 0:
            logger.warning(f"⚠️ {remaining} trades still OPEN after cleanup!")
        else:
            logger.info("✅ Database fully cleaned!")
        
        conn.close()
        return remaining == 0
        
    except Exception as e:
        logger.error(f"❌ Database error: {e}")
        return False

async def main():
    print("=" * 60)
    print("  FUNDING BOT - FULL CLEANUP")
    print("=" * 60)
    print()
    
    # Step 1: Force close Lighter positions
    print("STEP 1: Closing all Lighter positions (IOC orders)...")
    print("-" * 60)
    lighter_ok = await force_close_lighter_positions()
    print()
    
    # Step 2: Clean database
    print("STEP 2: Cleaning database...")
    print("-" * 60)
    db_ok = clean_database()
    print()
    
    # Summary
    print("=" * 60)
    print("  CLEANUP COMPLETE")
    print("=" * 60)
    print(f"  Lighter Positions: {'✅ CLOSED' if lighter_ok else '⚠️ CHECK MANUALLY'}")
    print(f"  Database:          {'✅ CLEANED' if db_ok else '⚠️ CHECK MANUALLY'}")
    print()
    print("You can now restart the bot with:")
    print("  python scripts/monitor_funding_final.py")
    print()

if __name__ == "__main__":
    asyncio.run(main())
