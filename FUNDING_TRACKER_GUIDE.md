# ═══════════════════════════════════════════════════════════════════════════════

# FUNDING TRACKER INTEGRATION GUIDE

# ═══════════════════════════════════════════════════════════════════════════════

## Overview

The Funding Tracker monitors realized funding payments from both exchanges and updates
the database with actual income generated from positions.

## Database Schema

The `funding_collected` column already exists in the `trades` table:

```sql
funding_collected REAL DEFAULT 0
```

## API Methods Used

### X10 Exchange

- **Method**: `fetch_open_positions()`
- **Field**: `realised_pnl` - includes accumulated funding fees
- **Type**: Float (positive = received, negative = paid)

### Lighter Exchange

- **Method**: `fetch_open_positions()`
- **Field**: `total_funding_paid_out` - cumulative funding paid
- **Type**: String (needs conversion to float)
- **Sign**: Negative (paid OUT means we received on the other side)

## Integration Steps

### 1. Import the Module

Add to your main script imports:

```python
from src.funding_tracker import create_funding_tracker
```

### 2. Initialize the Tracker

After database and adapters are ready, create the tracker:

```python
# Create funding tracker (updates every hour)
funding_tracker = await create_funding_tracker(
    x10_adapter=x10,
    lighter_adapter=lighter,
    trade_repository=trade_repo,
    update_interval_hours=1.0  # Update every 1 hour
)
```

### 3. Add to Shutdown Sequence

Ensure clean shutdown:

```python
async def shutdown():
    logger.info("Shutting down...")

    # Stop funding tracker
    await funding_tracker.stop()

    # ... other shutdown tasks
```

## Usage Example

```python
import asyncio
from src.database import AsyncDatabase, TradeRepository, DBConfig
from src.funding_tracker import create_funding_tracker

async def main():
    # Initialize database
    db = AsyncDatabase(DBConfig(db_path="data/trades.db"))
    await db.initialize()
    trade_repo = TradeRepository(db)

    # Initialize exchange adapters (your existing code)
    x10 = X10Adapter(...)
    lighter = LighterAdapter(...)

    # Create and start funding tracker
    tracker = await create_funding_tracker(
        x10_adapter=x10,
        lighter_adapter=lighter,
        trade_repository=trade_repo,
        update_interval_hours=1.0
    )

    # Your main trading loop runs here
    # Tracker runs in background automatically

    try:
        while True:
            # Your trading logic
            await asyncio.sleep(60)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await tracker.stop()
        await db.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
```

## Logging Output

The tracker provides detailed logging:

```
✅ Funding Tracker started (updates every 3600s)
📊 Updating funding for 3 open trades...
💰 BTC-PERP: Collected $12.50 funding (total: $87.50)
💰 ETH-PERP: Collected $8.30 funding (total: $45.20)
✅ Funding update complete: 2/3 trades updated, $20.80 collected this cycle ($1234.56 lifetime) in 2.3s
```

## Monitoring

Get real-time stats:

```python
stats = tracker.get_stats()
print(f"Total collected: ${stats['total_funding_collected']:.2f}")
print(f"Last update: {stats['last_update_time']}")
print(f"Errors: {stats['errors']}")
```

## Database Queries

Check funding collected for specific trade:

```python
trade = await trade_repo.get_trade_by_symbol("BTC-PERP")
print(f"Funding collected: ${trade['funding_collected']:.2f}")
```

Get total funding across all closed trades:

```python
result = await db.fetch_one(
    "SELECT SUM(funding_collected) as total FROM trades WHERE status = 'closed'"
)
print(f"Total funding collected: ${result['total']:.2f}")
```

## Configuration

Adjust update frequency based on funding rate intervals:

- **1 hour**: Standard (8 funding payments per day)
- **30 minutes**: More frequent updates
- **4 hours**: Less frequent (saves API calls)

```python
tracker = await create_funding_tracker(
    ...,
    update_interval_hours=0.5  # 30 minutes
)
```

## Troubleshooting

### Issue: Funding amounts seem incorrect

**Solution**: Check the sign convention:

- X10: `realised_pnl` is cumulative (includes funding)
- Lighter: `total_funding_paid_out` is what you PAID (negate it)

### Issue: No funding updates

**Solution**:

1. Check if positions exist: `await adapter.fetch_open_positions()`
2. Verify API authentication for both exchanges
3. Check logs for API errors

### Issue: Negative funding collected

**Explanation**: This is normal! It means you're paying funding (short when market is bullish)

- Positive = receiving funding (you're on the right side)
- Negative = paying funding (you're on the wrong side)

## Performance Notes

- Runs in background (non-blocking)
- Uses existing adapter methods (no new API endpoints)
- Database writes are queued (write-behind pattern)
- Minimal impact on trading loop
- Error handling with automatic retry

## Next Steps

1. Monitor logs for first few cycles
2. Verify database updates with SQL queries
3. Adjust `update_interval_hours` based on needs
4. Add custom alerts for large funding payments
5. Export funding data for tax reporting
