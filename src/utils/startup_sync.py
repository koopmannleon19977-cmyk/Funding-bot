"""
Startup Reconciliation Script

Synchronizes database state with actual exchange positions before trading starts.
Fixes 3 types of inconsistencies:
1. Missing in DB: Recover trades that exist on exchange but not in DB
2. Ghost in DB: Close trades marked 'open' in DB but closed on exchange
3. Size Mismatch: Update DB to match actual exchange position size

Run ONCE at bot startup after adapters are initialized.
"""

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

from src.utils import safe_float


async def synchronize_state(x10_adapter, lighter_adapter, trade_repository, db) -> dict[str, int]:
    """
    Synchronize database state with actual exchange positions.

    Args:
        x10_adapter: X10 exchange adapter
        lighter_adapter: Lighter exchange adapter
        trade_repository: Database repository for trades
        db: Database connection

    Returns:
        Dict with reconciliation statistics:
        {
            'recovered': int,  # Trades recovered from exchange
            'closed': int,     # Ghost trades closed
            'updated': int     # Size mismatches fixed
        }
    """
    logger.info("🔄 Starting database reconciliation with exchange state...")

    stats = {"recovered": 0, "closed": 0, "updated": 0}

    try:
        # ═══════════════════════════════════════════════════════════════════
        # STEP 1: Fetch Real State from Exchanges
        # ═══════════════════════════════════════════════════════════════════
        logger.info("📊 Fetching positions from exchanges...")

        x10_positions = await _fetch_positions(x10_adapter, "X10")
        lighter_positions = await _fetch_positions(lighter_adapter, "Lighter")

        # Build lookup: symbol -> {exchange -> position_data}
        exchange_state = {}

        for pos in x10_positions:
            symbol = pos["symbol"]
            if symbol not in exchange_state:
                exchange_state[symbol] = {}
            exchange_state[symbol]["x10"] = pos

        for pos in lighter_positions:
            symbol = pos["symbol"]
            if symbol not in exchange_state:
                exchange_state[symbol] = {}
            exchange_state[symbol]["lighter"] = pos

        logger.info(f"✅ Exchange state: {len(exchange_state)} symbols with open positions")

        # ═══════════════════════════════════════════════════════════════════
        # STEP 2: Fetch DB State (Open Trades)
        # ═══════════════════════════════════════════════════════════════════
        logger.info("📚 Fetching open trades from database...")

        db_trades = await _fetch_db_trades(db)
        db_symbols = {trade["symbol"] for trade in db_trades}

        logger.info(f"✅ Database state: {len(db_trades)} open trades")

        # ═══════════════════════════════════════════════════════════════════
        # STEP 3: Reconciliation Logic
        # ═══════════════════════════════════════════════════════════════════

        # Case 1: Recover Missing Trades (Exchange has position, DB doesn't)
        exchange_symbols = set(exchange_state.keys())
        missing_in_db = exchange_symbols - db_symbols

        if missing_in_db:
            logger.warning(f"⚠️ Found {len(missing_in_db)} positions on exchange but missing in DB: {missing_in_db}")

            for symbol in missing_in_db:
                positions = exchange_state[symbol]

                # Check if we have both legs (we need both for arbitrage)
                if "x10" in positions and "lighter" in positions:
                    recovered = await _recover_trade(
                        symbol=symbol, x10_pos=positions["x10"], lighter_pos=positions["lighter"], db=db
                    )

                    if recovered:
                        stats["recovered"] += 1
                        logger.info(f"✅ Recovered trade: {symbol}")
                else:
                    # Only one leg - this is an orphan position (should be closed manually)
                    legs = list(positions.keys())
                    logger.error(
                        f"❌ {symbol}: Orphan position detected (only on {legs})! "
                        f"Manual intervention required - close position on exchange."
                    )

        # Case 2: Close Ghost Trades (DB says open, but exchange shows no position)
        for trade in db_trades:
            symbol = trade["symbol"]

            # Check if symbol exists in exchange state
            if symbol not in exchange_state:
                # No position on either exchange - close in DB
                await _close_ghost_trade(symbol, trade, db)
                stats["closed"] += 1
                logger.info(f"✅ Closed ghost trade: {symbol} (no exchange position)")
                continue

            positions = exchange_state[symbol]

            # Case 3: Size Mismatch (both exist but sizes differ)
            await _reconcile_sizes(symbol, trade, positions, db, stats)

        # ═══════════════════════════════════════════════════════════════════
        # Summary
        # ═══════════════════════════════════════════════════════════════════
        logger.info(
            f"✅ Reconciliation complete:\n"
            f"   📥 Recovered: {stats['recovered']} trades\n"
            f"   🗑️ Closed: {stats['closed']} ghost trades\n"
            f"   🔄 Updated: {stats['updated']} size mismatches"
        )

        if stats["recovered"] + stats["closed"] + stats["updated"] == 0:
            logger.info("✨ Database state is clean - no corrections needed!")

        return stats

    except Exception as e:
        logger.error(f"❌ Reconciliation failed: {e}", exc_info=True)
        return stats


async def _fetch_positions(adapter, exchange_name: str) -> list[dict]:
    """
    Fetch open positions from an exchange adapter.

    Returns:
        List of position dictionaries with keys:
        - symbol: str
        - size: float (signed: positive=long, negative=short)
        - side: str ('BUY' or 'SELL')
        - notional_usd: float
    """
    try:
        raw_positions = await adapter.fetch_open_positions()

        if not raw_positions:
            logger.debug(f"{exchange_name}: No open positions")
            return []

        positions = []

        for pos in raw_positions:
            symbol = pos.get("symbol")
            size = safe_float(pos.get("size", 0), 0.0)

            # Skip zero-size positions
            if abs(size) < 1e-8:
                continue

            # Determine side from size sign
            side = "BUY" if size > 0 else "SELL"

            # Calculate notional (use mark price from adapter)
            mark_price = adapter.fetch_mark_price(symbol)
            notional_usd = abs(size) * (mark_price or 0)

            positions.append(
                {"symbol": symbol, "size": size, "side": side, "notional_usd": notional_usd, "exchange": exchange_name}
            )

        logger.info(f"{exchange_name}: {len(positions)} open positions")
        return positions

    except Exception as e:
        logger.error(f"Failed to fetch {exchange_name} positions: {e}")
        return []


async def _fetch_db_trades(db) -> list[dict]:
    """
    Fetch all open trades from database.

    Returns:
        List of trade dictionaries
    """
    try:
        sql = """
            SELECT 
                id, symbol, side_x10, side_lighter, 
                size_usd, entry_price_x10, entry_price_lighter,
                entry_time, status
            FROM trades 
            WHERE status = 'open'
        """

        async with db.connection() as conn, conn.execute(sql) as cursor:
            rows = await cursor.fetchall()

            trades = []
            for row in rows:
                trades.append(
                    {
                        "id": row[0],
                        "symbol": row[1],
                        "side_x10": row[2],
                        "side_lighter": row[3],
                        "size_usd": row[4],
                        "entry_price_x10": row[5],
                        "entry_price_lighter": row[6],
                        "entry_time": row[7],
                        "status": row[8],
                    }
                )

            return trades

    except Exception as e:
        logger.error(f"Failed to fetch DB trades: {e}")
        return []


async def _recover_trade(symbol: str, x10_pos: dict, lighter_pos: dict, db) -> bool:
    """
    Recover a missing trade by inserting it into the database.

    This happens when the exchange has an open position but the DB doesn't know about it.
    We reconstruct the trade data from the position info.
    """
    try:
        logger.info(f"🔧 Recovering missing trade: {symbol}")

        # Reconstruct trade data
        size_usd = (x10_pos["notional_usd"] + lighter_pos["notional_usd"]) / 2  # Average

        # Determine which exchange is long/short
        leg1_exchange = "X10" if x10_pos["side"] == "BUY" else "Lighter"

        sql = """
            INSERT INTO trades (
                symbol, side_x10, side_lighter, size_usd,
                entry_price_x10, entry_price_lighter,
                entry_time, status, is_farm_trade, account_label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 0, 'Recovered')
        """

        async with db.connection() as conn:
            await conn.execute(
                sql,
                (
                    symbol,
                    x10_pos["side"],
                    lighter_pos["side"],
                    size_usd,
                    x10_pos["notional_usd"] / abs(x10_pos["size"]),  # Approx entry price
                    lighter_pos["notional_usd"] / abs(lighter_pos["size"]),
                    datetime.now(UTC).isoformat(),
                ),
            )
            await conn.commit()

        logger.info(
            f"✅ Recovered {symbol}: X10 {x10_pos['side']} ${x10_pos['notional_usd']:.2f}, "
            f"Lighter {lighter_pos['side']} ${lighter_pos['notional_usd']:.2f}"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to recover {symbol}: {e}")
        return False


async def _close_ghost_trade(symbol: str, trade: dict, db) -> bool:
    """
    Close a ghost trade (exists in DB but not on exchange).

    Updates the trade status to 'closed' with zero PnL.
    """
    try:
        logger.info(f"🗑️ Closing ghost trade: {symbol} (no exchange position)")

        sql = """
            UPDATE trades 
            SET 
                status = 'closed',
                exit_time = ?,
                close_reason = 'Ghost trade - closed by reconciliation'
            WHERE symbol = ? AND status = 'open'
        """

        async with db.connection() as conn:
            await conn.execute(sql, (datetime.now(UTC).isoformat(), symbol))
            await conn.commit()

        logger.info(f"✅ Closed ghost: {symbol} (ID: {trade['id']})")
        return True

    except Exception as e:
        logger.error(f"Failed to close ghost {symbol}: {e}")
        return False


async def _reconcile_sizes(symbol: str, trade: dict, positions: dict, db, stats: dict):
    """
    Check if DB size matches exchange size and update if different.
    """
    try:
        db_size = trade["size_usd"]

        # Calculate actual size from exchange positions
        x10_notional = positions.get("x10", {}).get("notional_usd", 0)
        lighter_notional = positions.get("lighter", {}).get("notional_usd", 0)
        actual_size = (x10_notional + lighter_notional) / 2  # Average

        # Check for mismatch (>10% difference)
        if abs(db_size - actual_size) > (db_size * 0.1):
            logger.warning(
                f"⚠️ {symbol}: Size mismatch - DB: ${db_size:.2f}, "
                f"Exchange: ${actual_size:.2f} (diff: {abs(db_size - actual_size):.2f})"
            )

            # Update DB to match exchange
            sql = """
                UPDATE trades 
                SET size_usd = ?
                WHERE symbol = ? AND status = 'open'
            """

            async with db.connection() as conn:
                await conn.execute(sql, (actual_size, symbol))
                await conn.commit()

            stats["updated"] += 1
            logger.info(f"✅ Updated {symbol} size: ${db_size:.2f} → ${actual_size:.2f}")

    except Exception as e:
        logger.error(f"Failed to reconcile size for {symbol}: {e}")
