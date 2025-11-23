# config.py
import os
import sys
import logging
import io
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# MASTER-SCHALTER
# ============================================================
LIVE_TRADING = True
X10_DRY_RUN = False
LIGHTER_DRY_RUN = False

# ============================================================
# GEBÜHREN
# ============================================================
TAKER_FEE_X10 = 0.00025
MAKER_FEE_X10 = 0.00000
FEES_LIGHTER = 0.00000

# ============================================================
# BLACKLIST
# ============================================================
BLACKLIST_SYMBOLS = {
    "AERO-USD",
    "MEGA-USD",
}

# ============================================================
# POSITIONSGRÖSSEN & LIMITS
# ============================================================
DESIRED_NOTIONAL_USD = 16.0
MIN_POSITION_SIZE_USD = 16.0
MAX_NOTIONAL_USD = 20.0
MAX_TRADE_SIZE_USD = 20.0
MAX_OPEN_TRADES = 4

# ============================================================
# PROFIT-FILTER (EINSTIEG)
# ============================================================
MIN_APY_FILTER = 0.12
MIN_DAILY_PROFIT_FILTER = MIN_APY_FILTER / 365

DYNAMIC_MIN_APY_ENABLED = True
DYNAMIC_MIN_APY_MULTIPLIER = 1.1
MIN_APY_FALLBACK = 0.05

DYNAMIC_FEES_ENABLED = True

# ============================================================
# SMART SIZING (KELLY-LITE)
# ============================================================
POSITION_SIZE_MULTIPLIERS = {
    "high": 2.0,
    "medium": 1.5,
    "normal": 1.2,
    "low": 0.8
}

MAX_POSITION_SIZE_PCT = 0.10
MIN_POSITION_SIZE_PCT = 0.02

# ============================================================
# BTC CORRELATION THRESHOLDS
# ============================================================
BTC_STRONG_MOMENTUM_PCT = 5.0
BTC_MEDIUM_MOMENTUM_PCT = 3.0
BTC_WEAK_MOMENTUM_PCT = 1.5

SYMBOL_CONFIDENCE_BOOST = {
    "BTC": 0.15,
    "ETH": 0.10,
    "SOL": 0.10,
    "AVAX": 0.05,
    "ARB": 0.05,
    "OP": 0.05
}

# ============================================================
# RISIKO-FILTER
# ============================================================
MAX_SPREAD_FILTER_PERCENT = 0.15
MIN_FREE_MARGIN_PCT = 0.05
MAX_EXPOSURE_PCT = 5.0
MAX_VOLATILITY_PCT_24H = 50.0

# ============================================================
# EXIT-LOGIK
# ============================================================
FUNDING_FLIP_HOURS_THRESHOLD = 8
DYNAMIC_HOLD_MAX_DAYS = 7.0

DYNAMIC_STOP_LOSS_MULTIPLIER = 2.0
DYNAMIC_TAKE_PROFIT_MULTIPLIER = 3.0

# ============================================================
# ORDER EXECUTION
# ============================================================
ORDER_GUARDIAN_TIMEOUT_SECONDS = 10
ORDER_GUARDIAN_LEG2_RETRY = 1
ORDER_GUARDIAN_RETRY_DELAY_SECONDS = 1.0

ROLLBACK_DELAY_SECONDS = 3

# ============================================================
# EXCHANGE-SPEZIFISCH
# ============================================================
X10_MAX_SLIPPAGE_PCT = 0.6
X10_PRICE_EPSILON_PCT = 0.15

LIGHTER_MAX_SLIPPAGE_PCT = 0.6
LIGHTER_PRICE_EPSILON_PCT = 0.25
LIGHTER_ORDER_TIMEOUT_SECONDS = 8
LIGHTER_MAX_API_KEY_INDEX = -1

LIGHTER_ACCOUNT_INDEX = 60113
LIGHTER_API_KEY_INDEX = 3
LIGHTER_AUTO_ACCOUNT_INDEX = False

# ============================================================
# VOLUME FARM MODE
# ============================================================
VOLUME_FARM_MODE = False
FARM_NOTIONAL_USD = 50
FARM_RANDOM_SIZE_PCT = 0.25
FARM_MIN_HOLD_MINUTES = 15
FARM_MAX_HOLD_MINUTES = 120
FARM_HOLD_SECONDS = 600
FARM_MAX_CONCURRENT = 40
FARM_MIN_APY = 0.05
FARM_MAX_VOLATILITY_24H = 4.0
FARM_MAX_SPREAD_PCT = 0.15

# ============================================================
# SYSTEM
# ============================================================
CONCURRENT_REQUEST_LIMIT = 2
REFRESH_DELAY_SECONDS = 300
DB_FILE = "funding.db"
LOG_FILE = "funding_bot.log"
LOG_LEVEL = logging.DEBUG

# ============================================================
# API ENDPOINTS & KEYS
# ============================================================
LIGHTER_BASE_URL = "https://mainnet.zklighter.elliot.ai"
LIGHTER_PRIVATE_KEY = os.getenv("LIGHTER_PRIVATE_KEY")
LIGHTER_API_PRIVATE_KEY = os.getenv("LIGHTER_API_PRIVATE_KEY")

X10_API_BASE_URL = "https://api.starknet.extended.exchange"
X10_PRIVATE_KEY = os.getenv("X10_PRIVATE_KEY")
X10_PUBLIC_KEY = os.getenv("X10_PUBLIC_KEY")
X10_API_KEY = os.getenv("X10_API_KEY")
X10_VAULT_ID = os.getenv("X10_VAULT_ID")

# ============================================================
# LOGGING SETUP
# ============================================================
def setup_logging():
    if sys.platform == 'win32':
        os.environ['PYTHONIOENCODING'] = 'utf-8'
    
    logger = logging.getLogger()
    logger.setLevel(LOG_LEVEL)

    if logger.handlers:
        for h in list(logger.handlers):
            logger.removeHandler(h)

    log_format = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(log_format)

    console_stream = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding='utf-8',
        errors='replace',
        line_buffering=True
    )

    console_handler = logging.StreamHandler(console_stream)
    console_handler.setFormatter(log_format)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

# ============================================================
# HELPER FUNCTIONS
# ============================================================
def parse_int(value, default=None):
    try:
        if value is None:
            return default
        return int(str(value).strip())
    except Exception:
        return default

def parse_float(value, default=None):
    try:
        if value is None:
            return default
        return float(str(value).strip())
    except Exception:
        return default

def validate_runtime_config(logger=None):
    _logger = logger or logging.getLogger()

    vault_int = parse_int(X10_VAULT_ID)
    if X10_VAULT_ID and vault_int is None:
        _logger.warning(f"CONFIG: X10_VAULT_ID '{X10_VAULT_ID}' nicht numerisch.")
    elif vault_int is None:
        _logger.info("CONFIG: X10_VAULT_ID fehlt - Live-Trading deaktiviert.")

    if DESIRED_NOTIONAL_USD < MIN_POSITION_SIZE_USD:
        _logger.warning(
            f"CONFIG: DESIRED_NOTIONAL_USD ({DESIRED_NOTIONAL_USD}) < "
            f"MIN_POSITION_SIZE_USD ({MIN_POSITION_SIZE_USD})."
        )

    if LIVE_TRADING:
        missing = []
        for key_name, key_value in [
            ("X10_PRIVATE_KEY", X10_PRIVATE_KEY),
            ("X10_PUBLIC_KEY", X10_PUBLIC_KEY),
            ("X10_API_KEY", X10_API_KEY),
            ("X10_VAULT_ID", X10_VAULT_ID),
        ]:
            if not key_value or key_value.strip() == "":
                missing.append(key_name)
        if missing:
            _logger.error(f"CONFIG: LIVE_TRADING=True aber Keys fehlen: {', '.join(missing)}")

    _logger.info(" CONFIG VALIDIERUNG abgeschlossen.")