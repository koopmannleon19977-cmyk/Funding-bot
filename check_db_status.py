import sqlite3
import pandas as pd
from pathlib import Path

db_path = Path("data/trades.db")
if not db_path.exists():
    print("DB not found")
    exit()

conn = sqlite3.connect(db_path)
query = "SELECT symbol, status, created_at, closed_at, funding_collected FROM trades WHERE symbol IN ('RESOLV-USD', 'ENA-USD', 'TAO-USD', 'MNT-USD', 'FARTCOIN-USD')"
df = pd.read_sql_query(query, conn)
print(df)
conn.close()
