from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any

@dataclass(frozen=True)
class Event:
    """Base event type."""
    pass

@dataclass(frozen=True)
class TradeOpened(Event):
    symbol: str
    size_usd: float
    timestamp: datetime = datetime.now()

@dataclass(frozen=True)
class TradeClosed(Event):
    symbol: str
    pnl_usd: float
    timestamp: datetime = datetime.now()

@dataclass(frozen=True)
class CriticalError(Event):
    message: str
    details: Optional[Dict[str, Any]] = None
    timestamp: datetime = datetime.now()

@dataclass(frozen=True)
class NotificationEvent(Event):
    level: str  # 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
    message: str
    timestamp: datetime = datetime.now()

