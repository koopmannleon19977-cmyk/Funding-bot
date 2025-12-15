import sqlite3
from pathlib import Path
import datetime

db_path = Path("data/trades.db")
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
query = "SELECT symbol, status, created_at, closed_at, funding_collected FROM trades WHERE symbol IN ('RESOLV-USD', 'ENA-USD', 'TAO-USD', 'MNT-USD', 'FARTCOIN-USD') ORDER BY created_at DESC"
cursor.execute(query)
rows = cursor.fetchall()

print(f"{'Symbol':<15} {'Status':<10} {'Created':<25} {'Closed':<25} {'Funding'}")
print("-" * 85)
for row in rows:
    created = datetime.datetime.fromtimestamp(row[2]/1000).strftime('%Y-%m-%d %H:%M:%S')
    closed = datetime.datetime.fromtimestamp(row[3]/1000).strftime('%Y-%m-%d %H:%M:%S') if row[3] else 'None'
    print(f"{row[0]:<15} {row[1]:<10} {created:<25} {closed:<25} {row[4]}")

conn.close()
