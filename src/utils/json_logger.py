# src/utils/json_logger.py
"""
Structured JSON Logging für Grafana/ELK Integration.

B5 Implementation: Produziert JSON-formatierte Logs die von Log-Aggregatoren
(Grafana Loki, Elasticsearch, Datadog, etc.) geparst werden können.

Usage:
    from src.utils.json_logger import JSONLogger, log_trade, log_funding, log_error

    # Singleton logger
    json_log = JSONLogger.get_instance()

    # Convenience functions
    log_trade("EDEN-USD", "ENTRY", side="LONG", size=150.0, price=0.083)
    log_funding("EDEN-USD", x10=0.05, lighter=-0.02, net=0.03)
    log_error("WebSocket", "Connection timeout", retry_count=3)
"""

import json
import logging
import os
import threading
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


class LogLevel(str, Enum):
    """Log levels matching standard logging."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogCategory(str, Enum):
    """Kategorien für strukturierte Filterung."""

    TRADE = "trade"
    FUNDING = "funding"
    POSITION = "position"
    ORDER = "order"
    WEBSOCKET = "websocket"
    API = "api"
    RECONCILIATION = "reconciliation"
    SHUTDOWN = "shutdown"
    STARTUP = "startup"
    ERROR = "error"
    METRIC = "metric"
    HEALTH = "health"


class DecimalEncoder(json.JSONEncoder):
    """JSON Encoder der Decimal und andere Typen handlet."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        if hasattr(obj, "__dict__"):
            return str(obj)
        return super().default(obj)


class JSONLogger:
    """
    Singleton JSON Logger für strukturierte Logs.

    Features:
    - JSON-formatierte Logs für Log-Aggregatoren
    - Automatische Timestamps (ISO 8601)
    - Kategorisierung für einfaches Filtern
    - Thread-safe
    - Sensitive Data Masking
    """

    _instance: Optional["JSONLogger"] = None
    _lock = threading.Lock()

    # Patterns für Sensitive Data (API Keys, Private Keys)
    SENSITIVE_KEYS = {"api_key", "private_key", "secret", "token", "password"}

    def __init__(
        self,
        log_file: str | None = None,
        enabled: bool = True,
        min_level: LogLevel = LogLevel.INFO,
        include_standard_log: bool = True,
    ):
        """
        Initialize JSON Logger.

        Args:
            log_file: Path to JSON log file. If None, uses logs/funding_bot_json.log
            enabled: Whether JSON logging is enabled
            min_level: Minimum log level to record
            include_standard_log: Also log to standard Python logger
        """
        self.enabled = enabled
        self.min_level = min_level
        self.include_standard_log = include_standard_log
        self._file_handler: Any | None = None
        self._standard_logger = logging.getLogger("json_logger")

        if log_file is None:
            log_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
            os.makedirs(log_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d")
            log_file = os.path.join(log_dir, f"funding_bot_json_{timestamp}.jsonl")

        self.log_file = log_file

        if self.enabled:
            self._open_file()

    @classmethod
    def get_instance(
        cls, log_file: str | None = None, enabled: bool = True, min_level: LogLevel = LogLevel.INFO
    ) -> "JSONLogger":
        """Get or create singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(log_file=log_file, enabled=enabled, min_level=min_level)
            return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset singleton (for testing)."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.close()
                cls._instance = None

    def _open_file(self):
        """Open log file for appending."""
        try:
            self._file_handler = open(self.log_file, "a", encoding="utf-8")
        except Exception as e:
            self._standard_logger.error(f"Failed to open JSON log file: {e}")
            self._file_handler = None

    def close(self):
        """Close log file."""
        if self._file_handler:
            try:
                self._file_handler.close()
            except:
                pass
            self._file_handler = None

    def _mask_sensitive(self, data: dict[str, Any]) -> dict[str, Any]:
        """Mask sensitive data in log entries."""
        masked = {}
        for key, value in data.items():
            if key.lower() in self.SENSITIVE_KEYS:
                masked[key] = "***MASKED***"
            elif isinstance(value, dict):
                masked[key] = self._mask_sensitive(value)
            elif isinstance(value, str) and len(value) > 20 and key.lower().endswith(("key", "secret", "token")):
                masked[key] = "***MASKED***"
            else:
                masked[key] = value
        return masked

    def _should_log(self, level: LogLevel) -> bool:
        """Check if level should be logged."""
        level_order = [LogLevel.DEBUG, LogLevel.INFO, LogLevel.WARNING, LogLevel.ERROR, LogLevel.CRITICAL]
        return level_order.index(level) >= level_order.index(self.min_level)

    def log(self, category: LogCategory, event: str, level: LogLevel = LogLevel.INFO, **kwargs):
        """
        Log a structured JSON event.

        Args:
            category: Log category for filtering
            event: Event name/description
            level: Log level
            **kwargs: Additional fields to include
        """
        if not self.enabled or not self._should_log(level):
            return

        # Build log entry
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level.value,
            "category": category.value,
            "event": event,
        }

        # Add additional fields (masked)
        if kwargs:
            masked_kwargs = self._mask_sensitive(kwargs)
            entry["data"] = masked_kwargs

        # Write to JSON log file
        if self._file_handler:
            try:
                json_line = json.dumps(entry, cls=DecimalEncoder, ensure_ascii=False)
                self._file_handler.write(json_line + "\n")
                self._file_handler.flush()
            except Exception as e:
                self._standard_logger.error(f"JSON log write failed: {e}")

        # Also log to standard logger if enabled
        if self.include_standard_log:
            log_msg = f"[{category.value}] {event}"
            if kwargs:
                # Format key fields inline
                key_fields = {
                    k: v for k, v in kwargs.items() if k in ("symbol", "side", "size", "price", "pnl", "error")
                }
                if key_fields:
                    log_msg += f" | {key_fields}"

            log_func = getattr(self._standard_logger, level.value.lower(), self._standard_logger.info)
            log_func(log_msg)

    # =========================================================================
    # Convenience Methods für häufige Log-Typen
    # =========================================================================

    def trade_entry(self, symbol: str, side: str, size: float, price: float, exchange: str, **kwargs):
        """Log trade entry."""
        self.log(
            LogCategory.TRADE,
            "TRADE_ENTRY",
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            exchange=exchange,
            **kwargs,
        )

    def trade_exit(
        self,
        symbol: str,
        side: str,
        size: float,
        entry_price: float,
        exit_price: float,
        pnl: float,
        reason: str,
        **kwargs,
    ):
        """Log trade exit."""
        self.log(
            LogCategory.TRADE,
            "TRADE_EXIT",
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            reason=reason,
            **kwargs,
        )

    def funding_payment(self, symbol: str, x10_funding: float, lighter_funding: float, net_funding: float, **kwargs):
        """Log funding payment."""
        self.log(
            LogCategory.FUNDING,
            "FUNDING_PAYMENT",
            symbol=symbol,
            x10_funding=x10_funding,
            lighter_funding=lighter_funding,
            net_funding=net_funding,
            **kwargs,
        )

    def position_update(
        self, symbol: str, exchange: str, side: str, size: float, entry_price: float, unrealized_pnl: float, **kwargs
    ):
        """Log position update."""
        self.log(
            LogCategory.POSITION,
            "POSITION_UPDATE",
            symbol=symbol,
            exchange=exchange,
            side=side,
            size=size,
            entry_price=entry_price,
            unrealized_pnl=unrealized_pnl,
            **kwargs,
        )

    def order_placed(
        self,
        symbol: str,
        exchange: str,
        order_type: str,
        side: str,
        size: float,
        price: float,
        order_id: str | None = None,
        **kwargs,
    ):
        """Log order placement."""
        self.log(
            LogCategory.ORDER,
            "ORDER_PLACED",
            symbol=symbol,
            exchange=exchange,
            order_type=order_type,
            side=side,
            size=size,
            price=price,
            order_id=order_id,
            **kwargs,
        )

    def order_filled(self, symbol: str, exchange: str, order_id: str, fill_size: float, fill_price: float, **kwargs):
        """Log order fill."""
        self.log(
            LogCategory.ORDER,
            "ORDER_FILLED",
            symbol=symbol,
            exchange=exchange,
            order_id=order_id,
            fill_size=fill_size,
            fill_price=fill_price,
            **kwargs,
        )

    def websocket_event(self, name: str, event: str, **kwargs):
        """Log WebSocket event."""
        level = LogLevel.WARNING if event in ("DISCONNECTED", "ERROR", "UNHEALTHY") else LogLevel.INFO
        self.log(LogCategory.WEBSOCKET, f"WS_{event}", level=level, ws_name=name, **kwargs)

    def api_call(
        self,
        exchange: str,
        endpoint: str,
        method: str = "GET",
        status: int = 200,
        latency_ms: float | None = None,
        **kwargs,
    ):
        """Log API call."""
        level = LogLevel.WARNING if status >= 400 else LogLevel.DEBUG
        self.log(
            LogCategory.API,
            "API_CALL",
            level=level,
            exchange=exchange,
            endpoint=endpoint,
            method=method,
            status=status,
            latency_ms=latency_ms,
            **kwargs,
        )

    def error(self, component: str, message: str, exception: Exception | None = None, **kwargs):
        """Log error."""
        self.log(
            LogCategory.ERROR,
            "ERROR",
            level=LogLevel.ERROR,
            component=component,
            message=message,
            exception_type=type(exception).__name__ if exception else None,
            exception_msg=str(exception) if exception else None,
            **kwargs,
        )

    def metric(self, name: str, value: int | float, unit: str | None = None, **kwargs):
        """Log metric for monitoring."""
        self.log(
            LogCategory.METRIC,
            "METRIC",
            level=LogLevel.DEBUG,
            metric_name=name,
            metric_value=value,
            metric_unit=unit,
            **kwargs,
        )

    def health_check(self, component: str, healthy: bool, **kwargs):
        """Log health check result."""
        level = LogLevel.INFO if healthy else LogLevel.WARNING
        self.log(LogCategory.HEALTH, "HEALTH_CHECK", level=level, component=component, healthy=healthy, **kwargs)


# =============================================================================
# Module-Level Convenience Functions
# =============================================================================


def get_json_logger() -> JSONLogger:
    """Get the singleton JSON logger instance."""
    return JSONLogger.get_instance()


def log_trade(symbol: str, event: str, **kwargs):
    """Quick trade logging."""
    logger = get_json_logger()
    logger.log(LogCategory.TRADE, event, symbol=symbol, **kwargs)


def log_funding(symbol: str, x10: float, lighter: float, net: float, **kwargs):
    """Quick funding logging."""
    logger = get_json_logger()
    logger.funding_payment(symbol, x10, lighter, net, **kwargs)


def log_error(component: str, message: str, exception: Exception | None = None, **kwargs):
    """Quick error logging."""
    logger = get_json_logger()
    logger.error(component, message, exception, **kwargs)


def log_metric(name: str, value: int | float, unit: str | None = None, **kwargs):
    """Quick metric logging."""
    logger = get_json_logger()
    logger.metric(name, value, unit, **kwargs)


def log_ws_event(name: str, event: str, **kwargs):
    """Quick WebSocket event logging."""
    logger = get_json_logger()
    logger.websocket_event(name, event, **kwargs)
