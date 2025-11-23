# migrate_db_v2.py
import sqlite3

DB_FILE = "funding.db"

conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()

# Check if sample_size exists (old schema)
try:
    cursor.execute("SELECT sample_size FROM fee_stats LIMIT 1")
    # Old schema detected - migrate
    print("⚠️ Old schema detected, migrating...")
    
    # Create temp table with new schema
    cursor.execute("""
        CREATE TABLE fee_stats_new (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            avg_fee_rate REAL NOT NULL,
            sample_count INTEGER DEFAULT 1,
            last_updated TIMESTAMP,
            PRIMARY KEY (exchange, symbol)
        )
    """)
    
    # Copy data
    cursor.execute("""
        INSERT INTO fee_stats_new (exchange, symbol, avg_fee_rate, sample_count, last_updated)
        SELECT exchange, symbol, avg_fee_rate, sample_size, last_updated
        FROM fee_stats
    """)
    
    # Drop old, rename new
    cursor.execute("DROP TABLE fee_stats")
    cursor.execute("ALTER TABLE fee_stats_new RENAME TO fee_stats")
    
    conn.commit()
    print("✅ Migrated sample_size -> sample_count")
    
except sqlite3.OperationalError as e:
    if "no such column: sample_size" in str(e):
        print("✅ Already new schema (sample_count)")
    else:
        print(f"❌ Error: {e}")

conn.close()
print("✅ Migration complete")