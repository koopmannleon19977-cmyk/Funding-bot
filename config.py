# config.py
import os
import sys
import logging
import io
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# 🛠️ EASY CONFIG (HIER EINSTELLUNGEN ÄNDERN)
# ==============================================================================

# 1. POSITIONSGRÖSSE & KAPITAL
# ------------------------------------------------------------------------------
# Wie viel $ soll JEDER Trade groß sein? (Hebel wird automatisch berechnet)
# Beispiel: $500 pro Trade bei $270 Kapital = 10x Hebel nötig
# Beispiel: $1000 pro Trade bei $270 Kapital = ~4x Hebel (Nee, das klappt nicht, brauchst mehr Kapital!)
#
# Faustregel: (Position Size * Max Trades) / Dein Kapital = Benötigter Hebel
# $500 * 5 Trades = $2500 Total Exposure. Bei $270 Kapital -> 9.25x Hebel (OK)
# $1000 * 5 Trades = $5000 Total Exposure. Bei $500 Kapital -> 10x Hebel (OK)
DESIRED_NOTIONAL_USD = 500.0      # Dein Ziel: $500 pro Trade (oder $1000 etc.)
MAX_OPEN_TRADES = 5               # Wie viele Trades gleichzeitig?

# ⚠️ Hebel-Einstellung (Muss zur Positionsgröße passen!)
# Wenn du $1000 Trades willst und $500 auf dem Konto hast:
# Leverage = $1000 Notional / ($500 Balance / 5 Trades) = 10x
LEVERAGE_MULTIPLIER = 10.0

# 🔴 CIRCUIT BREAKER (NOT-AUS)
# ------------------------------------------------------------------------------
# Schaltet den Bot ab, wenn zu viele Fehler passieren oder zu viel Geld verloren geht.
CB_MAX_CONSECUTIVE_FAILURES = 5     # Nach 5 fehlgeschlagenen Trades in Folge -> STOP
CB_MAX_DRAWDOWN_PCT = 0.15          # Nach 15% Equity-Verlust in 1 Stunde -> STOP
CB_ENABLE_KILL_SWITCH = True        # Soll der Bot sich beenden? (Ja/Nein)
CB_DRAWDOWN_WINDOW = 3600           # Zeitraum für Drawdown (Sekunden)

# 2. STRATEGIE & PROFIT
# ------------------------------------------------------------------------------
MIN_APY_FILTER = 0.30       # 30% APY Minimum (0.30 = 30%)
MIN_APY_FALLBACK = 0.30     # Absolutes Minimum (nicht unterschreiten)
MIN_PROFIT_EXIT_USD = 0.02  # Schließen ab $0.02 Profit (nach Gebühren)

# 3. SICHERHEIT
# ------------------------------------------------------------------------------
MAX_SPREAD_FILTER_PERCENT = 0.003  # Max 0.3% Spread erlaubt
MAX_BREAKEVEN_HOURS = 4.0          # Trade muss in 4h profitabel sein
BALANCE_RESERVE_PCT = 0.03         # 3% des Kapitals immer frei lassen

# Blacklist (Coins die NIE getradet werden sollen)
BLACKLIST_SYMBOLS = {
    # "AERO-USD", 
    # "MEGA-USD",
}

# ==============================================================================
# ⚙️ SYSTEM CONFIG (NUR FÜR EXPERTEN)
# ==============================================================================

# --- Master Switch ---
LIVE_TRADING = True
X10_DRY_RUN = False
LIGHTER_DRY_RUN = False
EMERGENCY_CLOSE_ON_START = False  # Set True to panic close everything

# --- Fees (DO NOT CHANGE unless exchange fees change) ---
TAKER_FEE_X10 = 0.000225  # 0.0225%
MAKER_FEE_X10 = 0.0000    # 0.00%
FEES_LIGHTER = 0.00000    # 0.00%
# Legacy Fee variables for compatibility
TAKER_FEE = TAKER_FEE_X10
MAKER_FEE = MAKER_FEE_X10

# --- Advanced Limits ---
MIN_POSITION_SIZE_USD = 50.0
MIN_TRADE_SIZE_USD = 50.0  
MAX_NOTIONAL_USD = DESIRED_NOTIONAL_USD * 1.2  # Buffer +20%
MAX_TRADE_SIZE_USD = MAX_NOTIONAL_USD
MIN_SAFE_THRESHOLD = 0.03

# --- Farm Mode Settings ---
VOLUME_FARM_MODE = True
FARM_POSITION_SIZE_USD = DESIRED_NOTIONAL_USD
FARM_NOTIONAL_USD = DESIRED_NOTIONAL_USD
FARM_RANDOM_SIZE_PCT = 0.05
FARM_HOLD_SECONDS = 7200  # 2h hold
FARM_MAX_CONCURRENT = MAX_OPEN_TRADES
FARM_MIN_APY = MIN_APY_FILTER
FARM_MAX_SPREAD_PCT = MAX_SPREAD_FILTER_PERCENT
FARM_MAX_VOLATILITY_24H = 15.0
FARM_MIN_INTERVAL_SECONDS = 15
FARM_BURST_LIMIT = 10
FARM_MAX_CONCURRENT_ORDERS = 5

# --- Dynamic & Adaptive Settings ---
DYNAMIC_MIN_APY_ENABLED = True
DYNAMIC_MIN_APY_MULTIPLIER = 1.1
DYNAMIC_FEES_ENABLED = True
THRESHOLD_UPDATE_INTERVAL = 300
MIN_DAILY_PROFIT_FILTER = MIN_APY_FILTER / 365

# --- Risk Filters ---
MIN_FREE_MARGIN_PCT = 0.05
MAX_EXPOSURE_PCT = 10.0
MAX_VOLATILITY_PCT_24H = 50.0
MIN_OPEN_INTEREST_USD = 50000
MAX_OI_FRACTION = 0.05

# --- System & API ---
DB_FILE = "funding.db"
LOG_FILE = "funding_bot.log"
LOG_LEVEL = logging.DEBUG
CONCURRENT_REQUEST_LIMIT = 10
REFRESH_DELAY_SECONDS = 3
TRADE_COOLDOWN_SECONDS = 120

# Reconnect / Watchdog
WS_PING_INTERVAL = 14
WS_PING_TIMEOUT = 8
WS_RECONNECT_DELAY_INITIAL = 5
WS_RECONNECT_DELAY_MAX = 60

# --- Prediction & Confidence ---
SYMBOL_CONFIDENCE_BOOST = {
    "BTC": 0.15, "ETH": 0.12, "SOL": 0.10, 
    "ARB": 0.08, "AVAX": 0.08, "BNB": 0.07,
}
HIGH_RISK_SYMBOLS = {"HYPE-USD", "MEME-USD", "PEPE-USD", "DOGE-USD"}
REBATE_PAIRS = {"BTC-USD", "ETH-USD", "SOL-USD", "ARB-USD", "AVAX-USD"}
REBATE_PREFIXES = ("BTC", "ETH", "SOL")

# Rebate defaults (Legacy)
REBATE_TRADES_PER_DAY = 3
REBATE_MAX_ANNUAL_DISCOUNT = 0.0
REBATE_MIN_ANNUAL_DISCOUNT = 0.0

# --- Order Execution ---
ORDER_GUARDIAN_TIMEOUT_SECONDS = 10
ORDER_GUARDIAN_LEG2_RETRY = 1
ORDER_GUARDIAN_RETRY_DELAY_SECONDS = 1.0
ROLLBACK_DELAY_SECONDS = 3
PARALLEL_EXECUTION_TIMEOUT = 15.0   # Timeout for parallel execution logic

X10_MAX_SLIPPAGE_PCT = 0.6
X10_PRICE_EPSILON_PCT = 0.15
LIGHTER_MAX_SLIPPAGE_PCT = 0.6
LIGHTER_PRICE_EPSILON_PCT = 0.25
LIGHTER_ORDER_TIMEOUT_SECONDS = 8

# API Keys
LIGHTER_BASE_URL = "https://mainnet.zklighter.elliot.ai"
LIGHTER_PRIVATE_KEY = os.getenv("LIGHTER_PRIVATE_KEY")
LIGHTER_API_PRIVATE_KEY = os.getenv("LIGHTER_API_PRIVATE_KEY")
LIGHTER_ACCOUNT_INDEX = 60113
LIGHTER_API_KEY_INDEX = 3
LIGHTER_AUTO_ACCOUNT_INDEX = False
LIGHTER_MAX_API_KEY_INDEX = -1

X10_API_BASE_URL = "https://api.starknet.extended.exchange"
X10_PRIVATE_KEY = os.getenv("X10_PRIVATE_KEY")
X10_PUBLIC_KEY = os.getenv("X10_PUBLIC_KEY")
X10_API_KEY = os.getenv("X10_API_KEY")
X10_VAULT_ID = os.getenv("X10_VAULT_ID")

TELEGRAM_ENABLED = False
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Arbitrage Disable
ENABLE_LATENCY_ARB = False

# Shutdown
CLOSE_ALL_ON_SHUTDOWN = True
SHUTDOWN_CLOSE_TIMEOUT = 60

# Kelly Sizing (Advanced)
POSITION_SIZE_MULTIPLIERS = {"high": 2.0, "medium": 1.5, "normal": 1.2, "low": 0.8}
MAX_POSITION_SIZE_PCT = 2.0
MIN_POSITION_SIZE_PCT = 0.02
KELLY_SAFETY_FACTOR = 0.25
MAX_SINGLE_TRADE_RISK_PCT = 0.10
MIN_KELLY_SAMPLE_SIZE = 10

# BTC Correlation
BTC_STRONG_MOMENTUM_PCT = 5.0
BTC_MEDIUM_MOMENTUM_PCT = 3.0
BTC_WEAK_MOMENTUM_PCT = 1.5

# ==============================================================================
# 4. CENTRALIZED MAGIC NUMBERS (NEW)
# ==============================================================================

# Timeouts & Delays
SLEEP_SHORT = 0.3               # Standard loop delay (0.3s)
SLEEP_LONG = 3.0                # Longer wait (e.g. after error)
WS_RECONNECT_DELAY = 5.0        # Delay before reconnecting WS

# Prediction Weights (v2)
PRED_WEIGHT_FUNDING = 0.6       # 60% weight on APY/Funding
PRED_WEIGHT_MOMENTUM = 0.3      # 30% weight on Momentum
PRED_WEIGHT_VOLATILITY = 0.1    # 10% weight on Volatility

# Prediction Confidence Weights (Detail)
PRED_CONF_WEIGHT_DIVERGENCE = 0.30
PRED_CONF_WEIGHT_IMBALANCE = 0.25
PRED_CONF_WEIGHT_OI = 0.20
PRED_CONF_WEIGHT_TREND = 0.15

# Monitoring Intervals
ZOMBIE_CHECK_INTERVAL = 300     # Check for zombie trades every 5 minutes
SYNC_CHECK_INTERVAL = 60        # Sync check every minute
HEALTH_CHECK_INTERVAL = 60      # Health report every minute

# Trading Thresholds (Aliases/New)
MIN_PROFIT_THRESHOLD = 0.02     # Minimum profit in USD to hold/close
SAFETY_MARGIN = 1.05            # Safety margin for calculations (5% buffer)

# ==============================================================================
# 5. COMPLIANCE & SAFETY (Phase 3)
# ==============================================================================
COMPLIANCE_CHECK_ENABLED = True     # If True, checks for self-matches before trading
COMPLIANCE_BLOCK_SELF_MATCH = True  # If True, aborts trade if self-match detected
COMPLIANCE_CANCEL_CONFLICTS = False # If True, would cancel conflicting orders (risky, default off)

# ==============================================================================
# 6. API & DASHBOARD
# ==============================================================================
API_ENABLED = True
API_HOST = "0.0.0.0"
API_PORT = 8080

# ==============================================================================
# 🧩 HELPER FUNCTIONS & LOGGING (DO NOT TOUCH)
# ==============================================================================

def setup_logging(per_run: bool = False, run_id: str | None = None, timestamp_format: str = "%Y%m%d_%H%M%S"):
    import re
    
    # =========================================================================
    # SECURITY: Custom filter to mask API keys and sensitive data in ALL logs
    # =========================================================================
    class SensitiveDataFilter(logging.Filter):
        """Filter that masks sensitive data (API keys, secrets) in log messages."""
        
        # Patterns to match and mask
        SENSITIVE_PATTERNS = [
            # X-Api-Key header (exact or in dict repr)
            (re.compile(r"('X-Api-Key':\s*'?)([a-zA-Z0-9]{20,})('?)"), r"\1***MASKED***\3"),
            (re.compile(r'("X-Api-Key":\s*"?)([a-zA-Z0-9]{20,})("?)'), r'"X-Api-Key": "***MASKED***"'),
            # X10 SDK RequestHeader format: <RequestHeader.API_KEY: 'X-Api-Key'>: 'key'
            (re.compile(r"(<RequestHeader\.API_KEY:\s*'X-Api-Key'>:\s*')([a-zA-Z0-9]{20,})('?)"), r"\1***MASKED***\3"),
            # API key as value in headers dict
            (re.compile(r"(api[_-]?key['\"]?:\s*['\"]?)([a-zA-Z0-9]{16,})(['\"]?)", re.IGNORECASE), r"\1***MASKED***\3"),
            # Private keys (hex)
            (re.compile(r"(private[_-]?key['\"]?:\s*['\"]?)(0x[a-fA-F0-9]{32,})(['\"]?)", re.IGNORECASE), r"\1***MASKED***\3"),
            # Generic secrets/tokens
            (re.compile(r"(secret['\"]?:\s*['\"]?)([a-zA-Z0-9]{16,})(['\"]?)", re.IGNORECASE), r"\1***MASKED***\3"),
            (re.compile(r"(token['\"]?:\s*['\"]?)([a-zA-Z0-9]{16,})(['\"]?)", re.IGNORECASE), r"\1***MASKED***\3"),
            # Catch any 32-char hex string that looks like an API key in context
            (re.compile(r"('[a-fA-F0-9]{32}')"), r"'***MASKED***'"),
        ]
        
        def filter(self, record: logging.LogRecord) -> bool:
            # Mask sensitive data in the message
            original_msg = str(record.getMessage())
            masked_msg = original_msg
            
            for pattern, replacement in self.SENSITIVE_PATTERNS:
                masked_msg = pattern.sub(replacement, masked_msg)
            
            # Only modify if we actually masked something
            if masked_msg != original_msg:
                record.msg = masked_msg
                record.args = ()  # Clear args since we've already formatted
            
            return True  # Always allow the record through
    
    if sys.platform == 'win32':
        os.environ['PYTHONIOENCODING'] = 'utf-8'
    logger = logging.getLogger()
    logger.setLevel(LOG_LEVEL)
    if logger.handlers:
        for h in list(logger.handlers):
            logger.removeHandler(h)
    log_format = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
    
    # Create the sensitive data filter
    sensitive_filter = SensitiveDataFilter()
    
    if per_run:
        ts = datetime.now().strftime(timestamp_format)
        log_file = f"funding_bot_{run_id}_{ts}.log" if run_id else f"funding_bot_{ts}.log"
    else:
        log_file = LOG_FILE
        
    log_dir = os.path.dirname(log_file)
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass

    file_handler = logging.FileHandler(log_file, mode='a', encoding="utf-8")
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(log_format)
    file_handler.addFilter(sensitive_filter)  # SECURITY: Apply filter
    
    console_handler = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True))
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(log_format)
    console_handler.addFilter(sensitive_filter)  # SECURITY: Apply filter
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

def parse_int(value, default=None):
    try: return int(str(value).strip()) if value is not None else default
    except: return default

def parse_float(value, default=None):
    try: return float(str(value).strip()) if value is not None else default
    except: return default

def validate_runtime_config(logger=None):
    _logger = logger or logging.getLogger()
    vault_int = parse_int(X10_VAULT_ID)
    if X10_VAULT_ID and vault_int is None: _logger.warning(f"CONFIG: X10_VAULT_ID invalid")
    elif vault_int is None: _logger.info("CONFIG: Missing X10_VAULT_ID")

    if DESIRED_NOTIONAL_USD < MIN_POSITION_SIZE_USD:
        _logger.warning(f"CONFIG: DESIRED_NOTIONAL_USD too small ({DESIRED_NOTIONAL_USD})")

    if LIVE_TRADING:
        missing = [k for k, v in [("X10_PRIVATE_KEY", X10_PRIVATE_KEY), ("X10_PUBLIC_KEY", X10_PUBLIC_KEY), 
                                  ("X10_API_KEY", X10_API_KEY), ("X10_VAULT_ID", X10_VAULT_ID)] if not v]
        if missing: _logger.error(f"CONFIG: Missing keys for LIVE_TRADING: {missing}")

    _logger.info("CONFIG VALIDATION COMPLETED.")