import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("data/trades.db")

def check_eden():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    cur.execute("SELECT * FROM trades WHERE symbol = 'EDEN-USD' AND status = 'open'")
    row = cur.fetchone()
    
    if row:
        print("EDEN-USD Trade Details:")
        for key in row.keys():
            print(f"  {key}: {row[key]}")
    else:
        print("EDEN-USD not found or not open.")

if __name__ == "__main__":
    check_eden()
