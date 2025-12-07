"""
Quick test to verify Task 1.2: PnL Recording Fix
This script directly tests that PnL values are correctly persisted to the database.
"""
import asyncio
import sqlite3
import time
import sys
sys.path.insert(0, '.')

async def test_pnl_recording():
    print("=" * 60)
    print("TASK 1.2 VERIFICATION: PnL Recording Test")
    print("=" * 60)
    
    # Import after path setup
    from src.state_manager import InMemoryStateManager, TradeState, TradeStatus
    
    db_path = "data/trades.db"
    sm = InMemoryStateManager(db_path=db_path)
    
    print("\n1. Starting State Manager...")
    await sm.start()
    
    # Create a unique test symbol
    test_symbol = f"TEST-PNL-{int(time.time())}"
    test_pnl = 3.75
    test_funding = 0.42
    
    print(f"\n2. Creating test trade: {test_symbol}")
    
    # Create a test trade
    test_trade = TradeState(
        symbol=test_symbol,
        side_x10="BUY",
        side_lighter="SELL",
        size_usd=500.0,
        entry_price_x10=1.0,
        entry_price_lighter=1.0,
        status=TradeStatus.OPEN,
        created_at=int(time.time() * 1000) - 10000,  # 10 seconds ago
    )
    
    await sm.add_trade(test_trade)
    print(f"   ✅ Trade added to state")
    
    # Now close with PnL
    print(f"\n3. Closing trade with PnL=${test_pnl:.2f}, Funding=${test_funding:.2f}")
    await sm.close_trade(test_symbol, pnl=test_pnl, funding=test_funding)
    print(f"   ✅ Trade closed in state")
    
    # Wait for write-behind to flush
    print(f"\n4. Waiting for database write (2 seconds)...")
    await asyncio.sleep(2)
    
    # Force flush
    await sm._flush_writes()
    print(f"   ✅ Writes flushed")
    
    # Stop the state manager
    await sm.stop()
    print(f"\n5. State Manager stopped")
    
    # Now check the database directly
    print(f"\n6. Verifying in database...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT symbol, pnl, funding_collected, closed_at FROM trades WHERE symbol = ?",
        (test_symbol,)
    )
    row = cursor.fetchone()
    
    if row:
        db_symbol, db_pnl, db_funding, db_closed = row
        print(f"\n   Database Record:")
        print(f"   - Symbol: {db_symbol}")
        print(f"   - PnL: ${db_pnl}")
        print(f"   - Funding: ${db_funding}")
        print(f"   - Closed At: {db_closed}")
        
        # Verify
        pnl_correct = abs(db_pnl - test_pnl) < 0.01
        funding_correct = abs(db_funding - test_funding) < 0.01
        
        print(f"\n" + "=" * 60)
        if pnl_correct and funding_correct:
            print("✅ TASK 1.2 VERIFIED: PnL Recording is WORKING!")
            print(f"   Expected PnL: ${test_pnl:.2f} → Got: ${db_pnl:.2f}")
            print(f"   Expected Funding: ${test_funding:.2f} → Got: ${db_funding:.2f}")
        else:
            print("❌ TASK 1.2 FAILED: PnL values don't match!")
            print(f"   Expected PnL: ${test_pnl:.2f} → Got: ${db_pnl:.2f}")
            print(f"   Expected Funding: ${test_funding:.2f} → Got: ${db_funding:.2f}")
        print("=" * 60)
    else:
        print(f"   ❌ Trade not found in database!")
    
    # Cleanup test trade
    cursor.execute("DELETE FROM trades WHERE symbol = ?", (test_symbol,))
    conn.commit()
    conn.close()
    print(f"\n7. Test trade cleaned up from database")

if __name__ == "__main__":
    asyncio.run(test_pnl_recording())
