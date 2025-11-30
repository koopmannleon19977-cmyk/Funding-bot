# Configuration for funding-bot

# Confidence boost values per symbol
SYMBOL_CONFIDENCE_BOOST = {
    "BTC": 0.15,
    "ETH": 0.12,
    "SOL": 0.10,
    "ARB": 0.08,
    "AVAX": 0.08,
    "BNB": 0.07,
}
# config.py
import os
import sys
import logging
import io
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# MASTER-SCHALTER
# ============================================================
LIVE_TRADING = True
X10_DRY_RUN = False
LIGHTER_DRY_RUN = False

# ============================================================
# EMERGENCY: Close all positions on bot start
# Set to True if bot is stuck with too many open positions
# ============================================================
EMERGENCY_CLOSE_ON_START = False  # Set to True to force-close all

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

TRADE_COOLDOWN_SECONDS = 120

# ============================================================
# POSITIONSGRÖSSEN & LIMITS
# ============================================================
# ═════════════════════════════════════════════════════════════════════════════==
# OPTIMIERT für $60 Total Balance
# ═════════════════════════════════════════════════════════════════════════════==
DESIRED_NOTIONAL_USD = 8          # Kleiner starten
MIN_POSITION_SIZE_USD = 5.0       # API Minimum
MIN_TRADE_SIZE_USD = 5.0          # NEU: Explicit setzen
MAX_NOTIONAL_USD = 15.0           # Nicht zu groß
MAX_TRADE_SIZE_USD = 20.0         # Max pro Trade
MAX_OPEN_TRADES = 3               # Weniger parallel (war 5)

# Safety: Reserve 20% statt 30%
BALANCE_RESERVE_PCT = 0.20        # Gesenkt von 0.30

# ============================================================
# PROFIT-FILTER (EINSTIEG)
# ============================================================
MIN_APY_FILTER = 0.05  # lowered from 0.12 to 0.05
MIN_DAILY_PROFIT_FILTER = MIN_APY_FILTER / 365

DYNAMIC_MIN_APY_ENABLED = True
DYNAMIC_MIN_APY_MULTIPLIER = 1.1
MIN_APY_FALLBACK = 0.05

DYNAMIC_FEES_ENABLED = True

# ============================================================
# ADAPTIVE THRESHOLD SETTINGS
# ============================================================
THRESHOLD_UPDATE_INTERVAL = 300
REBATE_PAIRS = {"BTC-USD", "ETH-USD", "SOL-USD", "ARB-USD", "AVAX-USD"}
REBATE_PREFIXES = ("BTC", "ETH", "SOL")
HIGH_RISK_SYMBOLS = {"HYPE-USD", "MEME-USD", "PEPE-USD", "DOGE-USD"}

# Rebate calculation
TAKER_FEE = 0.00025
MAKER_FEE = 0.00000
REBATE_TRADES_PER_DAY = 3
REBATE_MAX_ANNUAL_DISCOUNT = 0.08
REBATE_MIN_ANNUAL_DISCOUNT = 0.005
MIN_SAFE_THRESHOLD = 0.03

# ============================================================
# ============================================================
# SMART SIZING (KELLY-LITE)
# ============================================================
POSITION_SIZE_MULTIPLIERS = {
    "high": 2.0,
    "medium": 1.5,
    "normal": 1.2,
    "low": 0.8
}

MAX_POSITION_SIZE_PCT = 0.10  # 10% of total balance
MIN_POSITION_SIZE_PCT = 0.02  # 2% of total balance

## Kelly Criterion Settings
KELLY_SAFETY_FACTOR = 0.25
MAX_SINGLE_TRADE_RISK_PCT = 0.10
MIN_KELLY_SAMPLE_SIZE = 10          # Mindest-Trades für volle Kelly-Berechnung

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
MAX_SPREAD_FILTER_PERCENT = 0.003
MIN_FREE_MARGIN_PCT = 0.05
MAX_EXPOSURE_PCT = 5.0
MAX_VOLATILITY_PCT_24H = 50.0

# ============================================================
# Open Interest & Funding Cache Settings
# ============================================================
# Open Interest Limits
MIN_OPEN_INTEREST_USD = 50000  # Minimum OI um zu traden (Liquidität)
MAX_OI_FRACTION = 0.02  # Max 2% des OI als Position

# Funding Rate Cache Settings
FUNDING_CACHE_TTL = 60  # Sekunden bis Funding Rate als "stale" gilt
FORCE_REST_FALLBACK_THRESHOLD = 10  # Wenn weniger als N Rates gecached → REST

# WebSocket Reconnect Settings
WS_RECONNECT_DELAY_INITIAL = 5
WS_RECONNECT_DELAY_MAX = 60
WS_PING_INTERVAL = 20
WS_PING_TIMEOUT = 10

# ============================================================
# EXIT-LOGIK
# ============================================================
FUNDING_FLIP_HOURS_THRESHOLD = 2
DYNAMIC_HOLD_MAX_DAYS = 2.0

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
FARM_NOTIONAL_USD = 11
FARM_RANDOM_SIZE_PCT = 0.25
FARM_MIN_HOLD_MINUTES = 15
FARM_MAX_HOLD_MINUTES = 120
FARM_HOLD_SECONDS = 60  # 1 min statt 2 min (drastisch reduziert für schnellere exits)
FARM_MAX_CONCURRENT = 2  # Reduced parallel farm trades (was 3)
FARM_MIN_APY = 0.03
FARM_MAX_VOLATILITY_24H = 4.0
FARM_MAX_SPREAD_PCT = 0.15

# Volume Farm Rate Limiting
FARM_MIN_INTERVAL_SECONDS = 15  # Min time between farm trades
FARM_BURST_LIMIT = 10  # Max trades per minute
FARM_MAX_CONCURRENT_ORDERS = 5  # Max parallel farm orders

# ============================================================
# SYSTEM
# ============================================================
CONCURRENT_REQUEST_LIMIT = 10  # Increased concurrency for aggressive scanning (2 -> 10)
REFRESH_DELAY_SECONDS = 3  # Less aggressive refresh (was 1)
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
# TELEGRAM BOT
# ============================================================
# Telegram deaktiviert bis Token gesetzt
TELEGRAM_ENABLED = False
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "") if os.getenv("TELEGRAM_BOT_TOKEN") else None
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "") if os.getenv("TELEGRAM_CHAT_ID") else None

# ============================================================
# LOGGING SETUP
# ============================================================
def setup_logging(per_run: bool = False, run_id: str | None = None, timestamp_format: str = "%Y%m%d_%H%M%S"):
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

    # determine filename: default `LOG_FILE` or per-run timestamped file
    if per_run:
        ts = datetime.now().strftime(timestamp_format)
        if run_id:
            log_file = f"funding_bot_{run_id}_{ts}.log"
        else:
            log_file = f"funding_bot_{ts}.log"
    else:
        log_file = LOG_FILE

    # ensure directory exists when a path with directory is provided
    log_dir = os.path.dirname(log_file)
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass

    file_handler = logging.FileHandler(log_file, mode='a', encoding="utf-8")
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(log_format)

    console_stream = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding='utf-8',
        errors='replace',
        line_buffering=True
    )

    console_handler = logging.StreamHandler(console_stream)
    console_handler.setLevel(LOG_LEVEL)
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