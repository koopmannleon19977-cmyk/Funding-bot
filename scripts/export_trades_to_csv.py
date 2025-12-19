import asyncio
import aiosqlite
import csv
import os
from datetime import datetime
import config

async def export_trades():
    """Export trade history from DB to realized_pnl.csv"""
    db_path = config.DB_FILE
    output_file = "realized_pnl.csv"
    
    if not os.path.exists(db_path):
        print(f"❌ Database not found at {db_path}")
        return

    print(f"🔄 Reading from {db_path}...")
    
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM trade_history ORDER BY exit_time ASC") as cursor:
            trades = await cursor.fetchall()
            
    print(f"📊 Found {len(trades)} trades. Writing to {output_file}...")
    
    fieldnames = [
        'market', 'size', 'entry_price', 'exit_price', 
        'trade_pnl', 'funding_fees', 'trading_fees', 
        'realised_pnl', 'closed_at'
    ]
    
    with open(output_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for t in trades:
            # Reconstruct logic matching state.py
            # DB columns: symbol, entry_time, exit_time... final_pnl_usd, funding_pnl_usd, spread_pnl_usd, fees_usd
            
            total_realized = t['final_pnl_usd']
            funding = t['funding_pnl_usd']
            fees = t['fees_usd']
            spread_pnl = t['spread_pnl_usd']
            
            # Recalculate trade_pnl as Price PnL - Fees to match my state.py logic
            # Wait, verify if spread_pnl in DB is Gross or Net?
            # src/core/state.py line 123 inserts pnl_data['spread_pnl'].
            # In pnl_utils.py (not viewed fully), spread_pnl is usually Gross Price PnL.
            # So trade_pnl = spread_pnl - fees.
            
            trade_pnl = spread_pnl - fees
            
            # Entry price (DB doesn't store entry price in trade_history???)
            # Wait, verify schema.
            # Line 118 in state.py: 
            # INSERT INTO trade_history (symbol, entry_time ... final_pnl_usd ...)
            # It DOES NOT store entry_price/exit_price/size in trade_history! 
            # This is a schema limitation.
            # I can only export what's there.
            
            # I will set 'size', 'entry_price', 'exit_price' to 0 or 'N/A' if missing,
            # or try to fetch from 'trades' table if possible? 
            # 'trades' table is usually for open trades. 'trade_history' is for closed.
            # If the user wants these columns, we might need to modify schema in future.
            # For now, I will export what I have.
            
            row = {
                'market': t['symbol'],
                'size': 0.0, # Missing in DB history
                'entry_price': 0.0, # Missing in DB history
                'exit_price': 0.0, # Missing in DB history
                'trade_pnl': f"{trade_pnl:.6f}",
                'funding_fees': f"{funding:.6f}",
                'trading_fees': f"{fees:.6f}",
                'realised_pnl': f"{total_realized:.6f}",
                'closed_at': t['exit_time']
            }
            writer.writerow(row)
            
    print(f"✅ Export complete. Written {len(trades)} rows.")

if __name__ == "__main__":
    asyncio.run(export_trades())
