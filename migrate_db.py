# migrate_db.py
import sqlite3

DB_FILE = "funding.db"

conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()

# Add missing column
try:
    cursor.execute("ALTER TABLE trades ADD COLUMN account_label TEXT DEFAULT 'Main'")
    conn.commit()
    print("✅ Added account_label column")
except sqlite3.OperationalError as e:
    if "duplicate column" in str(e):
        print("✅ Column already exists")
    else:
        raise

conn.close()
print("✅ Migration complete")