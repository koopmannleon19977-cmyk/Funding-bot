"""
Trade event storage helpers.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from datetime import datetime
from decimal import Decimal
from typing import Any, get_args, get_origin

from funding_bot.domain.events import AlertEvent, DomainEvent
from funding_bot.domain.models import Exchange, ExecutionState, Side, TradeStatus


async def append_event(self, trade_id: str, event: DomainEvent) -> None:
    """Append an event to the trade's audit log."""
    await self._write_queue.put({
        "action": "append_event",
        "data": {
            "event_id": event.event_id,
            "trade_id": trade_id,
            "event_type": event.event_type,
            "timestamp": event.timestamp.isoformat(),
            "payload": json.dumps(dataclasses.asdict(event), default=str),
        },
    })


async def list_events(
    self,
    trade_id: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[DomainEvent]:
    """List events with optional filters."""
    if not self._conn:
        return []

    conditions: list[str] = []
    params: list[Any] = []

    if trade_id:
        conditions.append("trade_id = ?")
        params.append(trade_id)
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if since:
        conditions.append("timestamp >= ?")
        params.append(since.isoformat())

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    cursor = await self._conn.execute(
        f"""
        SELECT event_id, trade_id, event_type, timestamp, payload
        FROM trade_events
        WHERE {where}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        params,
    )
    rows = await cursor.fetchall()

    # Import lazily to avoid circular import costs at startup.
    from funding_bot.domain import events as domain_events

    def _parse_dt(value: str) -> datetime:
        # SQLite stores ISO strings; normalize "Z" to "+00:00" for fromisoformat.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
        if value is None:
            return default
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            return default

    def _coerce(annotation: Any, value: Any) -> Any:
        origin = get_origin(annotation)
        if origin is None:
            # Fast paths
            if annotation is Decimal:
                return _to_decimal(value)
            if annotation is float:
                try:
                    return float(value)
                except Exception:
                    return 0.0
            if annotation is int:
                try:
                    return int(value)
                except Exception:
                    return 0
            if annotation in (Exchange, Side, TradeStatus, ExecutionState):
                if isinstance(value, str) and "." in value:
                    value = value.split(".")[-1]
                return annotation(value)
            if annotation is datetime:
                if isinstance(value, str):
                    return _parse_dt(value)
                return value
            return value

        args = get_args(annotation)
        if origin is list and isinstance(value, list):
            inner = args[0] if args else Any
            return [_coerce(inner, v) for v in value]
        if origin is dict and isinstance(value, dict):
            return value
        if origin is tuple and isinstance(value, (list, tuple)):
            return tuple(value)

        # Union / Optional
        if origin is types.UnionType or origin is typing.Union:
            if value is None:
                return None
            for candidate in args:
                if candidate is type(None):
                    continue
                try:
                    return _coerce(candidate, value)
                except Exception:
                    continue
            return value

        return value

    events: list[DomainEvent] = []
    for event_id, row_trade_id, row_type, ts_str, payload_str in rows:
        try:
            payload = json.loads(payload_str) if payload_str else {}
        except Exception:
            payload = {}

        timestamp = _parse_dt(ts_str)
        cls = getattr(domain_events, row_type, None)
        if not isinstance(cls, type) or not issubclass(cls, DomainEvent):
            events.append(
                AlertEvent(
                    event_id=event_id,
                    timestamp=timestamp,
                    level="INFO",
                    message=f"Unknown event type: {row_type}",
                    details={"trade_id": row_trade_id, "payload": payload},
                )
            )
            continue

        kwargs: dict[str, Any] = {}
        for field in dataclasses.fields(cls):
            if field.name in ("event_id", "timestamp"):
                continue
            if field.name in payload:
                kwargs[field.name] = _coerce(field.type, payload[field.name])

        try:
            events.append(cls(event_id=event_id, timestamp=timestamp, **kwargs))
        except Exception as e:
            events.append(
                AlertEvent(
                    event_id=event_id,
                    timestamp=timestamp,
                    level="WARNING",
                    message=f"Failed to parse event: {row_type}",
                    details={
                        "trade_id": row_trade_id,
                        "error": str(e),
                        "payload": payload,
                    },
                )
            )

    return events
