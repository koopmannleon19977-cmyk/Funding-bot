"""
Structured logging setup.

Provides both text and JSON logging with sensitive data masking.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from funding_bot.config.settings import Settings


class SensitiveDataFilter(logging.Filter):
    """Filter that masks sensitive data (API keys, secrets) in log messages."""

    SENSITIVE_PATTERNS = [
        (re.compile(r"('X-Api-Key':\s*'?)([a-zA-Z0-9]{20,})('?)"), r"\1***MASKED***\3"),
        (re.compile(r'("X-Api-Key":\s*"?)([a-zA-Z0-9]{20,})("?)'), r'"X-Api-Key": "***MASKED***"'),
        (re.compile(r"(api[_-]?key['\"]?:\s*['\"]?)([a-zA-Z0-9]{16,})(['\"]?)", re.IGNORECASE), r"\1***MASKED***\3"),
        (re.compile(r"(private[_-]?key['\"]?:\s*['\"]?)(0x[a-fA-F0-9]{32,})(['\"]?)", re.IGNORECASE), r"\1***MASKED***\3"),
        (re.compile(r"(secret['\"]?:\s*['\"]?)([a-zA-Z0-9]{16,})(['\"]?)", re.IGNORECASE), r"\1***MASKED***\3"),
        (re.compile(r"(token['\"]?:\s*['\"]?)([a-zA-Z0-9]{16,})(['\"]?)", re.IGNORECASE), r"\1***MASKED***\3"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        original_msg = str(record.getMessage())
        masked_msg = original_msg

        for pattern, replacement in self.SENSITIVE_PATTERNS:
            masked_msg = pattern.sub(replacement, masked_msg)

        if masked_msg != original_msg:
            record.msg = masked_msg
            record.args = ()

        return True


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder for Decimal and other types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        if hasattr(obj, "__dict__"):
            return str(obj)
        return super().default(obj)


class JSONFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add extra fields if present
        for key in ["trade_id", "symbol", "exchange", "state", "order_id"]:
            if hasattr(record, key):
                log_data[key] = getattr(record, key)

        return json.dumps(log_data, cls=DecimalEncoder)


def setup_logging(settings: Settings | None = None) -> logging.Logger:
    """
    Set up logging with both console and file handlers.

    Returns the root logger.
    """
    if settings is None:
        from funding_bot.config.settings import get_settings
        settings = get_settings()

    # Get log level
    level = getattr(logging, settings.logging.level.upper(), logging.INFO)
    if settings.testing_mode and level > logging.DEBUG:
        level = logging.DEBUG

    # Create root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create sensitive data filter
    sensitive_filter = SensitiveDataFilter()

    # Console handler (text format)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    # Use custom formatter for console
    console_format = BotLogFormatter()
    console_handler.setFormatter(console_format)
    console_handler.addFilter(sensitive_filter)
    root_logger.addHandler(console_handler)

    # File handler (text format)
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_handler = logging.FileHandler(
        logs_dir / f"funding_bot_{timestamp}.log",
        encoding="utf-8"
    )
    file_handler.setLevel(level)

    # Keep standard format for files (no colors, full timestamp)
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    file_handler.setFormatter(file_fmt)
    file_handler.addFilter(sensitive_filter)
    root_logger.addHandler(file_handler)

    # JSON file handler (if enabled)
    if settings.logging.json_enabled:
        json_path = Path(settings.logging.json_file)
        json_path.parent.mkdir(parents=True, exist_ok=True)

        max_bytes = int(getattr(settings.logging, "json_max_bytes", 0) or 0)
        backup_count = int(getattr(settings.logging, "json_backup_count", 0) or 0)
        if max_bytes > 0 and backup_count > 0:
            json_handler = RotatingFileHandler(
                json_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        else:
            json_handler = logging.FileHandler(json_path, encoding="utf-8")
        json_handler.setLevel(level)
        json_handler.setFormatter(JSONFormatter())
        json_handler.addFilter(sensitive_filter)
        root_logger.addHandler(json_handler)

    # Reduce noise from verbose libraries
    for lib in ["websockets", "asyncio", "aiosqlite", "urllib3", "httpcore"]:
        logging.getLogger(lib).setLevel(logging.WARNING)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a named logger."""
    return logging.getLogger(name)


class BotLogFormatter(logging.Formatter):
    """
    Custom formatter for console output with colors and simplified structure.

    Levels:
    - INFO: Green/White
    - WARNING: Yellow
    - ERROR: Red
    - CRITICAL: Bold Red
    """

    # ANSI Colors
    RESET = "\033[0m"
    GREY = "\033[90m"
    WHITE = "\033[97m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD_RED = "\033[1;91m"
    CYAN = "\033[96m"
    BLUE = "\033[94m"

    FORMATS = {
        logging.DEBUG: f"{GREY}%(asctime)s [DEBUG] %(message)s{RESET}",
        logging.INFO: f"{GREEN}%(asctime)s [INFO]{RESET} %(message)s",
        logging.WARNING: f"{YELLOW}%(asctime)s [WARN] %(message)s{RESET}",
        logging.ERROR: f"{RED}%(asctime)s [ERROR] %(message)s{RESET}",
        logging.CRITICAL: f"{BOLD_RED}%(asctime)s [CRITICAL] %(message)s{RESET}",
    }

    def __init__(self):
        super().__init__(datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        # Special handling for trade events or specific keywords to pop
        msg = record.getMessage()

        # Colorize specific keywords in message for better scanning
        if "[TRADE]" in msg or "[PROFIT]" in msg:
             log_fmt = f"{self.CYAN}%(asctime)s [TRADE]{self.RESET} %(message)s"
             record.msg = msg.replace("[TRADE]", "").replace("[PROFIT]", "").strip()
        elif "[HEALTH]" in msg:
             log_fmt = f"{self.BLUE}%(asctime)s [HEALTH]{self.RESET} %(message)s"
             # Strip the redundant [HEALTH] tag from message if present to avoid duplication
             record.msg = msg.replace("[HEALTH]", "").strip()
        elif "[SCAN]" in msg:
             # Make scan logs grey/dim so they recede
             log_fmt = f"{self.GREY}%(asctime)s [SCAN]{self.RESET} %(message)s"
             record.msg = msg.replace("[SCAN]", "").strip()
        else:
            log_fmt = self.FORMATS.get(record.levelno, self.FORMATS[logging.INFO])

        formatter = logging.Formatter(log_fmt, datefmt="%H:%M:%S")
        return formatter.format(record)
