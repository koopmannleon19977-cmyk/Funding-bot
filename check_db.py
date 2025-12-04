import sqlite3
c = sqlite3.connect('data/trades.db')
cols = [r[1] for r in c.execute('PRAGMA table_info(trades)').fetchall()]
for i, col in enumerate(cols):
    print(f"{i}: {col}")
