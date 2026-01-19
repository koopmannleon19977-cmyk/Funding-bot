import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class TradeStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    ROLLBACK = "rollback"


@dataclass(slots=True)
class TradeState:
    """
    In-memory representation of a trade.

    Financial fields (size_usd, pnl, funding_collected) are stored as float
    for SQLite compatibility, but Decimal properties are provided for
    precision-critical calculations.

    OPTIMIZED: Uses slots=True for ~30% memory reduction per instance.
    This is the modern Python 3.10+ way to use __slots__ with dataclasses.
    """

    symbol: str
    side_x10: str
    side_lighter: str
    size_usd: float
    entry_price_x10: float = 0.0
    entry_price_lighter: float = 0.0
    status: TradeStatus = TradeStatus.OPEN
    is_farm_trade: bool = False
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    entry_time: datetime | None = field(default_factory=lambda: datetime.now(UTC))
    closed_at: int | None = None
    pnl: float = 0.0
    funding_collected: float = 0.0
    account_label: str = "Main"
    x10_order_id: str | None = None
    lighter_order_id: str | None = None

    # ═══════════════════════════════════════════════════════════════
    # OPTIONAL EXECUTION / PNL SNAPSHOT FIELDS (in-memory only)
    # These are NOT persisted in the trades table (safe defaults on reload).
    # Fee fields are stored as *fee rate* (e.g. 0.000225), unless a caller
    # explicitly uses them differently.
    # ═══════════════════════════════════════════════════════════════
    entry_qty_x10: float = 0.0
    entry_qty_lighter: float = 0.0
    entry_fee_x10: float | None = None
    entry_fee_lighter: float | None = None

    exit_qty_x10: float = 0.0
    exit_qty_lighter: float = 0.0
    exit_price_x10: float = 0.0
    exit_price_lighter: float = 0.0
    exit_fee_x10: float | None = None
    exit_fee_lighter: float | None = None

    x10_exit_order_id: str | None = None
    lighter_exit_order_id: str | None = None
    db_id: int | None = None  # Database row ID

    # ═══════════════════════════════════════════════════════════════
    # DECIMAL PROPERTIES FOR PRECISION-CRITICAL CALCULATIONS
    # ═══════════════════════════════════════════════════════════════

    @property
    def size_usd_decimal(self) -> Decimal:
        """Get size_usd as Decimal for precise calculations"""
        from src.utils import safe_decimal

        return safe_decimal(self.size_usd)

    @property
    def pnl_decimal(self) -> Decimal:
        """Get pnl as Decimal for precise calculations"""
        from src.utils import safe_decimal

        return safe_decimal(self.pnl)

    @property
    def funding_collected_decimal(self) -> Decimal:
        """Get funding_collected as Decimal for precise calculations"""
        from src.utils import safe_decimal

        return safe_decimal(self.funding_collected)

    @property
    def entry_price_x10_decimal(self) -> Decimal:
        """Get entry_price_x10 as Decimal"""
        from src.utils import safe_decimal

        return safe_decimal(self.entry_price_x10)

    @property
    def entry_price_lighter_decimal(self) -> Decimal:
        """Get entry_price_lighter as Decimal"""
        from src.utils import safe_decimal

        return safe_decimal(self.entry_price_lighter)

    def set_pnl_from_decimal(self, value: Decimal | float) -> None:
        """Set pnl from a Decimal value (converts to float for storage)"""
        from src.utils import quantize_usd, safe_decimal

        self.pnl = float(quantize_usd(safe_decimal(value)))

    def set_funding_from_decimal(self, value: Decimal | float) -> None:
        """Set funding_collected from a Decimal value"""
        from src.utils import quantize_usd, safe_decimal

        self.funding_collected = float(quantize_usd(safe_decimal(value)))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization"""
        from dataclasses import asdict

        data = asdict(self)
        data["status"] = self.status.value
        # Serialize entry_time as ISO string
        data["entry_time"] = self.entry_time.isoformat() if self.entry_time else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TradeState":
        """Create from dictionary with robust type handling"""
        import logging
        from datetime import datetime

        logger = logging.getLogger(__name__)

        data = data.copy()  # Don't modify original

        # Status conversion
        if isinstance(data.get("status"), str):
            try:
                data["status"] = TradeStatus(data["status"])
            except ValueError:
                data["status"] = TradeStatus.OPEN

        # Entry time conversion - ROBUST
        entry_time = data.get("entry_time")
        if entry_time is not None:
            if isinstance(entry_time, str):
                try:
                    # Handle various ISO formats
                    entry_time = entry_time.replace("Z", "+00:00")
                    if "." in entry_time and "+" in entry_time:
                        # Format: 2025-12-02T21:12:41.017751+00:00
                        data["entry_time"] = datetime.fromisoformat(entry_time)
                    elif "T" in entry_time:
                        # Format: 2025-12-02T21:12:41
                        data["entry_time"] = datetime.fromisoformat(entry_time)
                    else:
                        # Format: 2025-12-02 21:12:41
                        data["entry_time"] = datetime.strptime(entry_time, "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError) as e:
                    logger.warning(f"Could not parse entry_time '{entry_time}': {e}")
                    data["entry_time"] = datetime.now(UTC)
            elif isinstance(entry_time, (int, float)):
                # Unix timestamp
                data["entry_time"] = datetime.fromtimestamp(entry_time, tz=UTC)
            elif not isinstance(entry_time, datetime):
                data["entry_time"] = datetime.now(UTC)
        else:
            data["entry_time"] = datetime.now(UTC)

        # Ensure timezone awareness
        if data["entry_time"].tzinfo is None:
            data["entry_time"] = data["entry_time"].replace(tzinfo=UTC)

        # Filter to valid fields only
        valid_fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}

        return cls(**valid_fields)


@dataclass
class Position:
    symbol: str
    side: str  # 'LONG' or 'SHORT'
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    leverage: Decimal
    exchange: str


@dataclass
class OrderResult:
    success: bool
    order_id: str | None
    error_message: str | None = None
    filled_size: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    fee_paid: Decimal = Decimal("0")


class ExchangeAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Exchange name (e.g., 'X10', 'Lighter')"""
        pass

    @abstractmethod
    async def fetch_open_positions(self) -> list[Position]:
        """Fetch all open positions from the exchange."""
        pass

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: Decimal,
        price: Decimal | None = None,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> OrderResult:
        """Place an order on the exchange."""
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order."""
        pass

    @abstractmethod
    async def get_order_status(self, symbol: str, order_id: str) -> dict[str, Any]:
        """Get the status of an order."""
        pass

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> Decimal:
        """Fetch the current funding rate for a symbol."""
        pass

    @abstractmethod
    async def fetch_mark_price(self, symbol: str) -> Decimal:
        """Fetch the current mark price for a symbol."""
        pass

    @abstractmethod
    async def get_available_balance(self) -> Decimal:
        """Get the available balance for trading."""
        pass

    @abstractmethod
    async def fetch_fee_schedule(self) -> tuple[Decimal, Decimal]:
        """Fetch maker and taker fee rates."""
        pass

    @abstractmethod
    async def aclose(self):
        """Cleanup resources."""
        pass


class StateManagerInterface(ABC):
    @abstractmethod
    async def get_all_open_trades(self) -> list[Any]:
        pass

    @abstractmethod
    async def add_trade(self, trade: Any) -> str:
        pass

    @abstractmethod
    async def update_trade(self, symbol: str, updates: dict[str, Any]) -> bool:
        pass

    @abstractmethod
    async def close_trade(self, symbol: str, pnl: float | Decimal, funding: float | Decimal) -> bool:
        pass


class EventBusInterface(ABC):
    @abstractmethod
    async def publish(self, event: Any) -> None:
        """Publish an event."""
        pass
