import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml


@dataclass
class TradingSettings:
    min_apy_filter: Decimal


@dataclass
class LoggingSettings:
    level: str


@dataclass
class Settings:
    trading: TradingSettings
    logging: LoggingSettings


def _apply_env_override(settings: Settings) -> Settings:
    env_val = os.getenv("BOT_TRADING__MIN_APY_FILTER")
    if env_val is not None:
        settings.trading.min_apy_filter = Decimal(env_val)
    return settings


def load_settings() -> Settings:
    env = os.getenv("BOT_ENV", "development")
    path = Path(__file__).with_name(f"settings.{env}.yaml")
    if not path.exists():
        raise FileNotFoundError(f"Config file not found for env '{env}': {path}")
    data = yaml.safe_load(path.read_text()) or {}

    trading_cfg = data.get("trading", {})
    logging_cfg = data.get("logging", {})
    settings = Settings(
        trading=TradingSettings(min_apy_filter=Decimal(str(trading_cfg.get("min_apy_filter", "0")))),
        logging=LoggingSettings(level=str(logging_cfg.get("level", "INFO"))),
    )
    return _apply_env_override(settings)
