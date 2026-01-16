"""
SQLite schema migrations.
"""

from __future__ import annotations

from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


async def _apply_schema_migrations(self) -> None:
    """Apply additive schema migrations (safe on existing DBs)."""
    if not self._conn:
        return

    # Add columns if missing (additive migrations only).
    cursor = await self._conn.execute("PRAGMA table_info(trades)")
    rows = await cursor.fetchall()
    columns = {row[1] for row in rows}  # row[1] = column name

    if "high_water_mark" not in columns:
        logger.info("Applying schema migration: add trades.high_water_mark")
        await self._conn.execute(
            "ALTER TABLE trades ADD COLUMN high_water_mark DECIMAL DEFAULT '0'"
        )
        await self._conn.commit()

    if "current_apy" not in columns:
        logger.info("Applying schema migration: add trades.current_apy")
        await self._conn.execute(
            "ALTER TABLE trades ADD COLUMN current_apy DECIMAL DEFAULT '0'"
        )
        await self._conn.commit()

    if "current_lighter_rate" not in columns:
        logger.info("Applying schema migration: add trades.current_lighter_rate")
        await self._conn.execute(
            "ALTER TABLE trades ADD COLUMN current_lighter_rate DECIMAL DEFAULT '0'"
        )
        await self._conn.commit()

    if "current_x10_rate" not in columns:
        logger.info("Applying schema migration: add trades.current_x10_rate")
        await self._conn.execute(
            "ALTER TABLE trades ADD COLUMN current_x10_rate DECIMAL DEFAULT '0'"
        )
        await self._conn.commit()

    if "last_eval_at" not in columns:
        logger.info("Applying schema migration: add trades.last_eval_at")
        await self._conn.execute(
            "ALTER TABLE trades ADD COLUMN last_eval_at TEXT"
        )
        await self._conn.commit()

    # Migrate execution_attempts table for hedge latency columns
    cursor = await self._conn.execute("PRAGMA table_info(execution_attempts)")
    rows = await cursor.fetchall()
    attempt_columns = {row[1] for row in rows}

    for col in ["leg1_to_leg2_submit_ms", "leg1_to_leg2_ack_ms", "leg2_place_ack_ms"]:
        if col not in attempt_columns:
            logger.info(f"Applying schema migration: add execution_attempts.{col}")
            await self._conn.execute(
                f"ALTER TABLE execution_attempts ADD COLUMN {col} DECIMAL"
            )
            await self._conn.commit()

    # Migration: Slippage tracking columns for performance analytics
    slippage_columns = {
        "estimated_exit_pnl": "DECIMAL DEFAULT '0'",
        "actual_exit_pnl": "DECIMAL DEFAULT '0'",
        "exit_slippage_usd": "DECIMAL DEFAULT '0'",
        "exit_spread_pct": "DECIMAL DEFAULT '0'",
        "close_attempts": "INTEGER DEFAULT 0",
        "close_duration_seconds": "DECIMAL DEFAULT '0'",
    }
    for col, col_type in slippage_columns.items():
        if col not in columns:
            logger.info(f"Applying schema migration: add trades.{col}")
            await self._conn.execute(
                f"ALTER TABLE trades ADD COLUMN {col} {col_type}"
            )
            await self._conn.commit()

    # Migration: total_fees column for tracking total trade fees (leg1 + leg2)
    if "total_fees" not in columns:
        logger.info("Applying schema migration: add trades.total_fees")
        await self._conn.execute(
            "ALTER TABLE trades ADD COLUMN total_fees DECIMAL DEFAULT '0'"
        )
        await self._conn.commit()
