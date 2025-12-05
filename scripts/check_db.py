import sqlite3

conn = sqlite3.connect('data/trades.db')
cursor = conn.cursor()

# Show all open trades
cursor.execute("SELECT id, symbol, status FROM trades WHERE status = 'OPEN'")
rows = cursor.fetchall()
print("Open trades in DB:")
for row in rows:
    print(f"  ID={row[0]}, Symbol={row[1]}, Status={row[2]}")

if rows:
    print(f"\nTotal: {len(rows)} zombie trades found")
    print("\nTo clean them, run:")
    print("  python scripts/clean_db_zombies.py --fix")
else:
    print("\n✅ No zombie trades in DB")

conn.close()
