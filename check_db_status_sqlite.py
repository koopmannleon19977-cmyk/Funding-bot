import sqlite3
from pathlib import Path

db_path = Path("data/trades.db")
if not db_path.exists():
    print("DB not found")
    exit()

conn = sqlite3.connect(db_path)
cursor = conn.cursor()
query = "SELECT symbol, status, created_at, closed_at, funding_collected FROM trades WHERE symbol IN ('RESOLV-USD', 'ENA-USD', 'TAO-USD', 'MNT-USD', 'FARTCOIN-USD')"
cursor.execute(query)
rows = cursor.fetchall()

print(f"{'Symbol':<15} {'Status':<10} {'Created':<20} {'Closed':<20} {'Funding'}")
print("-" * 70)
for row in rows:
    print(f"{row[0]:<15} {row[1]:<10} {row[2]:<20} {row[3] or 'None':<20} {row[4]}")

conn.close()
