"""
Execution attempt (KPI) helpers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from funding_bot.adapters.store.sqlite.utils import _maybe_decimal, _safe_attempt_status
from funding_bot.domain.models import ExecutionAttempt, ExecutionAttemptStatus


async def create_execution_attempt(self, attempt: ExecutionAttempt) -> str:
    if not self._conn:
        raise RuntimeError("SQLite store not initialized")

    payload = json.dumps(attempt.data, default=str)
    await self._conn.execute(
        """
        INSERT INTO execution_attempts (
            attempt_id, trade_id, symbol, mode, status, stage, reason,
            apy, expected_value_usd, breakeven_hours, opp_spread_pct,
            entry_spread_pct, max_spread_pct, realized_spread_post_leg1_pct,
            leg1_fill_seconds, leg2_fill_seconds, leg1_slippage_bps, leg2_slippage_bps,
            total_fees, data_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt.attempt_id,
            attempt.trade_id,
            attempt.symbol,
            attempt.mode,
            attempt.status.value if hasattr(attempt.status, "value") else str(attempt.status),
            attempt.stage,
            attempt.reason,
            _maybe_decimal(attempt.data.get("apy")),
            _maybe_decimal(attempt.data.get("expected_value_usd")),
            _maybe_decimal(attempt.data.get("breakeven_hours")),
            _maybe_decimal(attempt.data.get("opp_spread_pct")),
            _maybe_decimal(attempt.data.get("entry_spread_pct")),
            _maybe_decimal(attempt.data.get("max_spread_pct")),
            _maybe_decimal(attempt.data.get("realized_spread_post_leg1_pct")),
            _maybe_decimal(attempt.data.get("leg1_fill_seconds")),
            _maybe_decimal(attempt.data.get("leg2_fill_seconds")),
            _maybe_decimal(attempt.data.get("leg1_slippage_bps")),
            _maybe_decimal(attempt.data.get("leg2_slippage_bps")),
            _maybe_decimal(attempt.data.get("total_fees")),
            payload,
            attempt.created_at.isoformat(),
            attempt.updated_at.isoformat(),
        ),
    )
    await self._conn.commit()
    return attempt.attempt_id


async def update_execution_attempt(self, attempt_id: str, updates: dict[str, Any]) -> bool:
    if not self._conn:
        return False

    # Fetch current payload (so we can merge data_json safely)
    cursor = await self._conn.execute(
        "SELECT data_json FROM execution_attempts WHERE attempt_id = ?",
        (attempt_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return False

    existing_data = {}
    try:
        existing_data = json.loads(row[0] or "{}")
    except Exception:
        existing_data = {}

    data_updates = updates.pop("data", None)
    if isinstance(data_updates, dict):
        existing_data.update(data_updates)

    # Allow callers to update common KPI keys directly too.
    for key in [
        "apy",
        "expected_value_usd",
        "breakeven_hours",
        "opp_spread_pct",
        "entry_spread_pct",
        "max_spread_pct",
        "realized_spread_post_leg1_pct",
        "leg1_fill_seconds",
        "leg2_fill_seconds",
        "leg1_slippage_bps",
        "leg2_slippage_bps",
        "total_fees",
    ]:
        if key in updates:
            existing_data[key] = updates[key]

    # Build SET clause
    set_parts: list[str] = ["data_json = ?", "updated_at = ?"]
    params: list[Any] = [json.dumps(existing_data, default=str), datetime.now(UTC).isoformat()]

    for column in ["trade_id", "symbol", "mode", "status", "stage", "reason"]:
        if column in updates and updates[column] is not None:
            set_parts.append(f"{column} = ?")
            value = updates[column]
            if column == "status" and isinstance(value, ExecutionAttemptStatus):
                value = value.value
            params.append(value)

    # Map common KPIs to columns
    kpi_map = {
        "apy": "apy",
        "expected_value_usd": "expected_value_usd",
        "breakeven_hours": "breakeven_hours",
        "opp_spread_pct": "opp_spread_pct",
        "entry_spread_pct": "entry_spread_pct",
        "max_spread_pct": "max_spread_pct",
        "realized_spread_post_leg1_pct": "realized_spread_post_leg1_pct",
        "leg1_fill_seconds": "leg1_fill_seconds",
        "leg2_fill_seconds": "leg2_fill_seconds",
        "leg1_slippage_bps": "leg1_slippage_bps",
        "leg2_slippage_bps": "leg2_slippage_bps",
        "total_fees": "total_fees",
        # Hedge latency KPIs
        "leg1_to_leg2_submit_ms": "leg1_to_leg2_submit_ms",
        "leg1_to_leg2_ack_ms": "leg1_to_leg2_ack_ms",
        "leg2_place_ack_ms": "leg2_place_ack_ms",
    }
    for key, column in kpi_map.items():
        if key in updates and updates[key] is not None:
            set_parts.append(f"{column} = ?")
            params.append(_maybe_decimal(updates[key]))

    params.append(attempt_id)

    cursor = await self._conn.execute(
        f"UPDATE execution_attempts SET {', '.join(set_parts)} WHERE attempt_id = ?",
        tuple(params),
    )
    await self._conn.commit()
    return cursor.rowcount > 0


async def list_execution_attempts(
    self,
    symbol: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[ExecutionAttempt]:
    if not self._conn:
        return []

    clauses: list[str] = []
    params: list[Any] = []

    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if since:
        clauses.append("created_at >= ?")
        params.append(since.isoformat())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cursor = await self._conn.execute(
        f"""
        SELECT attempt_id, trade_id, symbol, mode, status, stage, reason, data_json, created_at, updated_at
        FROM execution_attempts
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        tuple(params + [limit]),
    )
    rows = await cursor.fetchall()

    attempts: list[ExecutionAttempt] = []
    for row in rows:
        data = {}
        try:
            data = json.loads(row[7] or "{}")
        except Exception:
            data = {}

        attempts.append(
            ExecutionAttempt(
                attempt_id=row[0],
                trade_id=row[1],
                symbol=row[2],
                mode=row[3],
                status=_safe_attempt_status(row[4]),
                stage=row[5],
                reason=row[6],
                data=data,
                created_at=datetime.fromisoformat(row[8]),
                updated_at=datetime.fromisoformat(row[9]),
            )
        )

    return attempts


async def get_hedge_latency_stats(
    self,
    since: datetime | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Get aggregated hedge latency statistics from execution_attempts."""
    if not self._conn:
        return {}

    clauses: list[str] = ["leg1_to_leg2_submit_ms IS NOT NULL"]
    params: list[Any] = []

    if since:
        clauses.append("created_at >= ?")
        params.append(since.isoformat())
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)

    where = " AND ".join(clauses)

    # Get aggregate stats
    cursor = await self._conn.execute(
        f"""
        SELECT
            COUNT(*) as count,
            AVG(leg1_to_leg2_submit_ms) as avg_submit,
            AVG(leg1_to_leg2_ack_ms) as avg_ack,
            AVG(leg2_place_ack_ms) as avg_place_ack,
            MIN(leg1_to_leg2_submit_ms) as min_submit,
            MAX(leg1_to_leg2_submit_ms) as max_submit,
            MIN(leg1_to_leg2_ack_ms) as min_ack,
            MAX(leg1_to_leg2_ack_ms) as max_ack
        FROM execution_attempts
        WHERE {where}
        """,
        tuple(params),
    )
    row = await cursor.fetchone()

    if not row or row[0] == 0:
        return {
            "count": 0,
            "avg_submit_ms": Decimal("0"),
            "avg_ack_ms": Decimal("0"),
            "avg_place_ack_ms": Decimal("0"),
            "min_submit_ms": Decimal("0"),
            "max_submit_ms": Decimal("0"),
            "min_ack_ms": Decimal("0"),
            "max_ack_ms": Decimal("0"),
            "by_symbol": {},
        }

    # Get per-symbol breakdown
    cursor = await self._conn.execute(
        f"""
        SELECT
            symbol,
            COUNT(*) as count,
            AVG(leg1_to_leg2_submit_ms) as avg_submit,
            AVG(leg1_to_leg2_ack_ms) as avg_ack
        FROM execution_attempts
        WHERE {where}
        GROUP BY symbol
        ORDER BY count DESC
        """,
        tuple(params),
    )
    symbol_rows = await cursor.fetchall()

    by_symbol: dict[str, dict[str, Any]] = {}
    for srow in symbol_rows:
        by_symbol[srow[0]] = {
            "count": srow[1],
            "avg_submit_ms": Decimal(str(srow[2])) if srow[2] else Decimal("0"),
            "avg_ack_ms": Decimal(str(srow[3])) if srow[3] else Decimal("0"),
        }

    return {
        "count": row[0],
        "avg_submit_ms": Decimal(str(row[1])) if row[1] else Decimal("0"),
        "avg_ack_ms": Decimal(str(row[2])) if row[2] else Decimal("0"),
        "avg_place_ack_ms": Decimal(str(row[3])) if row[3] else Decimal("0"),
        "min_submit_ms": Decimal(str(row[4])) if row[4] else Decimal("0"),
        "max_submit_ms": Decimal(str(row[5])) if row[5] else Decimal("0"),
        "min_ack_ms": Decimal(str(row[6])) if row[6] else Decimal("0"),
        "max_ack_ms": Decimal(str(row[7])) if row[7] else Decimal("0"),
        "by_symbol": by_symbol,
    }
