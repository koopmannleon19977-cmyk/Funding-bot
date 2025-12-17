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
# ═══════════════════════════════════════════════════════════════════════════════
# WICHTIG: Min Notional auf beiden Exchanges beachten!
# - X10: Meist $10 min, aber manche Pairs brauchen $50+
# - Lighter: Meist $10-50 je nach Pair (via API: min_base_amount * price)
# 
# EMPFEHLUNG: Mindestens $60-100 pro Trade für zuverlässige Fills!
# Bei deinem Kapital ($208 X10 + $239 Lighter = ~$447 total):
# - ZIEL: 10% Gewinn/Monat ($45)
# - REALITÄT: Logs zeigen ~60% APY Opportunities (TAO, CRV, RESOLV)
# - STRATEGIE: 2x Leverage reicht völlig aus! (Sicherer als 4x)
# - RECHNUNG: $225 * 4 Trades = $900 Exposure. $900 * 60% APY = $540/Jahr = $45/Monat.
# ═══════════════════════════════════════════════════════════════════════════════
# 🔥 PROFITABILITY FIX (16.12.2025):
# - Größere Trades = weniger relativer Slippage
# - Weniger Trades = Qualität > Quantität
# ═══════════════════════════════════════════════════════════════════════════════
DESIRED_NOTIONAL_USD = 150.0     # ERHÖHT: $150 pro Trade (vorher $80)
MAX_OPEN_TRADES = 2               # REDUZIERT: Qualität > Quantität (vorher 4)
LEVERAGE_MULTIPLIER = 5.0         # 5x Limit is plenty for 2x Real Leverage
MAX_KRAKEN_TIME_DRIFT_SEC = 0.250

# ─────────────────────────────────────────────────────────────────────────────
# Maker micro-fill policy (Lighter maker leg)
#
# Problem: If Lighter maker partially fills below X10 min order size, the bot
# cannot hedge on X10 and is temporarily unhedged. These defaults aim to keep
# unhedged exposure bounded and resolve deterministically.
#
# Behavior:
# - Allow a short grace window to complete the fill.
# - If still unhedgeable and unhedged USD exceeds threshold, abort.
# - Always abort if max wait is exceeded.
# ─────────────────────────────────────────────────────────────────────────────
MAKER_MICROFILL_GRACE_SECONDS = 8.0
MAKER_MICROFILL_MAX_WAIT_SECONDS = 20.0
MAKER_MICROFILL_CHECK_INTERVAL_SECONDS = 1.0
MAKER_MICROFILL_MAX_UNHEDGED_USD = 5.0
# Burst Limit (New)
FARM_MAX_CONCURRENT_ORDERS = 3    # Max concurrent order launches per cycle (reduced to avoid rate limits)

# 🔴 CIRCUIT BREAKER (NOT-AUS)
# ------------------------------------------------------------------------------
# Schaltet den Bot ab, wenn zu viele Fehler passieren oder zu viel Geld verloren geht.
CB_MAX_CONSECUTIVE_FAILURES = 5     # Nach 5 fehlgeschlagenen Trades in Folge -> STOP
CB_MAX_DRAWDOWN_PCT = 0.20          # Relaxed to 20% for aggressive mode
CB_ENABLE_KILL_SWITCH = True        # Soll der Bot sich beenden? (Ja/Nein)
CB_DRAWDOWN_WINDOW = 3600           # Zeitraum für Drawdown (Sekunden)

# 2. STRATEGIE & PROFIT
# ------------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════════════════════════════
# PROFITABILITY FIX (16.12.2025):
# APY muss hoch genug sein um Fees + Slippage zu kompensieren!
# Breakeven bei $150 Trade: ~0.05% Roundtrip = braucht >35% APY
# ═════════════════════════════════════════════════════════════════════════════
MIN_APY_FILTER = 0.35       # ERHÖHT: 35% APY Minimum (vorher 20%) - kompensiert Fees!
MIN_APY_FALLBACK = 0.25     # ERHÖHT: 25% Fallback (vorher 15%)
MIN_PROFIT_EXIT_USD = 0.10  # REDUZIERT: $0.10 Minimum (bei größeren Trades reicht das)
MIN_MAINTENANCE_APY = 0.20  # ERHÖHT: Exit wenn APY < 20% (vorher 10%)
MAX_HOLD_HOURS = 72.0       # ERHÖHT: 72h max (vorher 48h) - mehr Zeit für Funding
EXIT_SLIPPAGE_BUFFER_PCT = 0.0015 # 0.15% Buffer for Bid/Ask Spread at Exit (erhöht für Sicherheit)

# ═════════════════════════════════════════════════════════════════════════════
# NEU: MINIMUM HOLD TIME - Verhindert zu frühes Schließen!
# Funding kommt alle 1h (BEIDE: Lighter + X10 zahlen stündlich)
# Du MUSST mindestens 2h halten um 2 Funding Payments zu bekommen!
# ═════════════════════════════════════════════════════════════════════════════
MINIMUM_HOLD_SECONDS = 7200       # NEU: 2 Stunden Mindest-Haltezeit!
MIN_FUNDING_BEFORE_EXIT_USD = 0.03  # NEU: Mindestens $0.03 Funding gesammelt

# ═════════════════════════════════════════════════════════════════════════════
# NEU: CROSS-EXCHANGE ARBITRAGE - Preisdifferenz-Profit!
# Wenn Coin A auf Börse A teurer ist als auf Börse B,
# kannst du durch die Preisdifferenz Profit machen (unabhängig von Funding)!
# Beispiel: Du hattest 2-3€ Profit durch Preisdifferenz bei EDEN-USD
# ═════════════════════════════════════════════════════════════════════════════
PRICE_DIVERGENCE_EXIT_ENABLED = True   # NEU: Exit bei Preisdifferenz-Profit
MIN_PRICE_DIVERGENCE_PROFIT_USD = 0.50 # NEU: Mindestens $0.50 Profit durch Preisdifferenz
PRICE_DIVERGENCE_EXIT_PCT = 0.005      # NEU: 0.5% Preisdifferenz = Exit Signal

# ═════════════════════════════════════════════════════════════════════════════
# ADVANCED EXIT OPTIMIZATION (16.12.2025)
# Dynamische Slippage-Berechnung statt statischem Buffer
# ═════════════════════════════════════════════════════════════════════════════
USE_DYNAMIC_SLIPPAGE = True            # NEU: Berechne echten Spread aus Orderbook
DYNAMIC_SLIPPAGE_FALLBACK_PCT = 0.002  # NEU: Fallback 0.2% wenn Orderbook nicht verfügbar
MIN_ORDERBOOK_DEPTH_USD = 100.0        # NEU: Mindestens $100 Liquidität im Orderbook
SMART_EXIT_ENABLED = True              # NEU: Nutze Orderbook für beste Exit-Preise
MAKER_EXIT_ENABLED = False             # NEU: Versuche Maker-Exit (0% Fee auf Lighter!)

# ═════════════════════════════════════════════════════════════════════════════
# PROFIT PROTECTION (Anti-Loss Safety)
# ═════════════════════════════════════════════════════════════════════════════
REQUIRE_POSITIVE_EXPECTED_PNL = True   # NEU: Nur Exit wenn PnL nach Kosten positiv!
MIN_NET_PROFIT_EXIT_USD = 0.05         # NEU: Mindestens $0.05 NET Profit nach allen Fees
EXIT_COST_SAFETY_MARGIN = 1.1          # NEU: 10% Safety Buffer auf Exit-Kosten

# 3. SICHERHEIT
# ------------------------------------------------------------------------------
MAX_SPREAD_FILTER_PERCENT = 0.002  # VERSCHÄRFT: 0.2% (vorher 0.3%) - weniger Slippage!
MAX_PRICE_IMPACT_PCT = 0.5         # H7: Max erlaubte Slippage aus Price Impact Simulation (0.5%)
MAX_BREAKEVEN_HOURS = 8.0          # REDUZIERT: Trade muss in 8h profitabel sein (vorher 12h)

# H8: Dynamic Spread Threshold (Volatility-based adjustments)
# Bei niedriger Vol: 0.75x stricter, bei hoher Vol: 1.5x relaxed (max 1%)
DYNAMIC_SPREAD_ENABLED = True      # H8: Aktiviert volatilitätsbasierte Spread-Limits

# B1: WebSocket Order Client (low-latency order submission)
# Orders werden über WS statt REST gesendet (~50-100ms schneller)
# Format korrigiert per offizieller Lighter Python SDK utils.py
LIGHTER_WS_ORDERS = True           # B1: WebSocket für Order Submission aktivieren

# ═══════════════════════════════════════════════════════════════════════════════
# TAKER ESCALATION (2025-12-17 Audit Fix)
# Nach Maker-Timeout zu Taker wechseln, ABER nur wenn EV noch positiv ist!
# ═══════════════════════════════════════════════════════════════════════════════
TAKER_ESCALATION_ENABLED = True              # Maker->Taker Escalation aktivieren
TAKER_ESCALATION_MAX_SLIPPAGE_PCT = 0.001    # 0.1% max Slippage für Taker (konservativ)
TAKER_ESCALATION_TIMEOUT_SECONDS = 10        # Timeout bevor Escalation (aktuell nicht verwendet)
MIN_TAKER_ESCALATION_PROFIT = 0.01           # $0.01 Minimum Profit nach Taker-Kosten

# ═══════════════════════════════════════════════════════════════════════════════
# VOLATILITY PANIC EXIT (2025-12-17 Audit Fix)
# Bei extremer Volatilität Position sofort schließen (Flash-Crash Protection)
# ═══════════════════════════════════════════════════════════════════════════════
VOLATILITY_PANIC_THRESHOLD = 8.0             # 8% = Panic Close (24h Range Volatility)
VOLATILITY_HARD_CAP_THRESHOLD = 50.0         # 50% = Absolutes Max (keine Entries)

# Blacklist (Coins die NIE getradet werden sollen)
# ═══════════════════════════════════════════════════════════════════════════════
# Gründe für Blacklist:
# - Invalid Margin Mode: Lighter unterstützt Cross/Isolated nicht für alle Assets
# - Ghost Positions: Orders fillen auf einer Exchange, aber nicht auf der anderen
# - Excessive Lag: X10 Updates viel langsamer → Arb Risk
# - Low Liquidity: Zu wenig Tiefe für zuverlässige Fills
# ═══════════════════════════════════════════════════════════════════════════════
BLACKLIST_SYMBOLS = {
    # Invalid Margin Mode on Lighter
    "XAU-USD", "XAG-USD", "USDJPY-USD",
    # Ghost Position / Excessive Lag
    "MEGA-USD",
    # Frequent failures / Low liquidity
    "S-USD",
    "LINEA-USD",  # Sehr kleine Fills in Trade Export (0.007628) = zu niedrige Liquidität
    # High Volatility Memes (optional - können profitabel sein, aber riskant)
    # "HYPE-USD", "MEME-USD", "PEPE-USD", "DOGE-USD",
}

# ==============================================================================
# ⚙️ SYSTEM CONFIG (NUR FÜR EXPERTEN)
# ==============================================================================

# --- Master Switch ---
LIVE_TRADING = True
X10_DRY_RUN = False
LIGHTER_DRY_RUN = False
EMERGENCY_CLOSE_ON_START = False  # Set True to panic close everything

# Global shutdown latch to block new orders during graceful shutdown
IS_SHUTTING_DOWN = False

# --- Fees (DO NOT CHANGE unless exchange fees change) ---
# X10 Exchange Fees
TAKER_FEE_X10 = 0.000225  # 0.0225%
MAKER_FEE_X10 = 0.0000    # 0.00%

# Lighter Exchange Fees (Standard Account = 0%, Premium = 0.002% Maker / 0.02% Taker)
# Docs: https://apidocs.lighter.xyz/docs/account-types
MAKER_FEE_LIGHTER = 0.0   # 0.00% (Standard Account)
TAKER_FEE_LIGHTER = 0.0   # 0.00% (Standard Account)

# Legacy Fee variables for compatibility
TAKER_FEE = TAKER_FEE_X10
MAKER_FEE = MAKER_FEE_X10

# --- Advanced Limits ---
# ═══════════════════════════════════════════════════════════════════════════════
# WICHTIG: DESIRED_NOTIONAL_USD MUSS >= MIN_POSITION_SIZE_USD sein!
# Sonst werden alle Trades mit "too small" abgelehnt!
# ═══════════════════════════════════════════════════════════════════════════════
MIN_POSITION_SIZE_USD = 50.0      # Hard minimum for any position (exchange limits)
MIN_TRADE_SIZE_USD = 50.0         # Hard minimum for any trade  
MAX_NOTIONAL_USD = DESIRED_NOTIONAL_USD * 1.5  # Buffer +50%
MAX_TRADE_SIZE_USD = 600.0        # Hard cap per single trade (increased for leverage)
MIN_SAFE_THRESHOLD = 0.05         # 5% minimum threshold (was 3%)

# --- Funding Trackin300.0        # Hard cap per single trade
# FundingTracker runs periodically and fetches realized funding payments.
# For debugging, reduce this to e.g. 30 to see cycles quickly.
FUNDING_TRACK_INTERVAL_SECONDS = 30

# Extra verbose FundingTracker logs (timestamps, scheduling, per-trade windows)
FUNDING_TRACKER_DEBUG = True

# --- Farm Mode Settings ---
# ═══════════════════════════════════════════════════════════════════════════════
# VOLUME FARM MODE: Sinnvoll für Rebates/Points auf beiden Exchanges!
# - X10: Trading Points für Volume
# - Lighter: Fee Rebates für Maker Volume
# 
# EMPFEHLUNG: True wenn du Rebate Tiers aufbauen willst
# ═══════════════════════════════════════════════════════════════════════════════
VOLUME_FARM_MODE = False          # DISABLED - using regular arb trades only
FARM_POSITION_SIZE_USD = DESIRED_NOTIONAL_USD
FARM_NOTIONAL_USD = DESIRED_NOTIONAL_USD
FARM_RANDOM_SIZE_PCT = 0.10       # 10% size variation (was 5% - more natural)
FARM_HOLD_SECONDS = 7200          # 2h hold (was 4h - faster rotation = more volume)
FARM_MAX_CONCURRENT = MAX_OPEN_TRADES
FARM_MIN_APY = MIN_APY_FILTER
FARM_MAX_SPREAD_PCT = MAX_SPREAD_FILTER_PERCENT
FARM_MAX_VOLATILITY_24H = 12.0    # Stricter (was 15% - avoid volatile coins)
FARM_MIN_INTERVAL_SECONDS = 30    # 30s minimum between farm trades (was 15s - rate limit friendly)
FARM_BURST_LIMIT = 5              # Max 5 trades per burst (was 10 - rate limit friendly)

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

# --- Latency Arbitrage (Experimental) ---
LATENCY_ARB_ENABLED = True
LATENCY_ARB_MIN_LAG_SECONDS = 5.0
LATENCY_ARB_MAX_LAG_SECONDS = 60.0    # Increased to 60s to tolerate quiet markets
LATENCY_ARB_MIN_RATE_DIFF = 0.0002    # 0.02% rate diff required

# --- System & API ---
DB_FILE = "data/funding.db"
LOG_FILE = "logs/funding_bot.log"
LOG_LEVEL = logging.INFO  # Changed from DEBUG to INFO to reduce log spam
CONCURRENT_REQUEST_LIMIT = 10
REFRESH_DELAY_SECONDS = 3
TRADE_COOLDOWN_SECONDS = 120

# --- Structured JSON Logging (B5: For Grafana/ELK Integration) ---
JSON_LOGGING_ENABLED = True                    # Master switch for JSON logging
JSON_LOG_FILE = "logs/funding_bot_json.jsonl"  # JSON Lines format (one JSON per line)
JSON_LOG_MIN_LEVEL = "INFO"                    # Minimum level: DEBUG, INFO, WARNING, ERROR

# Reconnect / Watchdog (Enhanced for 1006 disconnect handling)
WS_PING_INTERVAL = 15              # Default ping interval for X10
WS_PING_TIMEOUT = 10               # Ping timeout 
WS_RECONNECT_DELAY_INITIAL = 2     # Reduced from 5 to 2 for faster reconnect
WS_RECONNECT_DELAY_MAX = 120       # Increased from 60 to 120 for server recovery
WS_MAX_RECONNECT_ATTEMPTS = 10     # Max attempts before alerting (0 = infinite)
WS_HEALTH_CHECK_INTERVAL = 30      # Health check every 30 seconds
WS_1006_EXTENDED_DELAY = 30        # Extended delay after repeated 1006 errors
WS_1006_ERROR_THRESHOLD = 3        # Trigger extended delay after this many consecutive 1006s

# ═══════════════════════════════════════════════════════════════════════════════
# LIGHTER WEBSOCKET SETTINGS (Based on Discord Community Info)
# ═══════════════════════════════════════════════════════════════════════════════
# 
# WebSocket Error 1006 = "Abnormal Closure" - connection closed without close frame
# 
# KRITISCH (aus Lighter Discord):
# - Der SERVER sendet UNS {"type":"ping"} alle ~60 Sekunden
# - WIR müssen mit {"type":"pong"} antworten
# - Wenn wir NICHT antworten → "no pong" / connection rejected / 1006!
# - Wir senden KEINE eigenen Pings - der Server antwortet nicht darauf!
#
# Die Connection wird aktiv gehalten durch:
# 1. Unsere Pong-Antworten auf Server-Pings
# 2. Die regelmäßigen Daten-Messages (Orderbook updates, etc.)
# 3. Unsere Subscriptions
#
# Docs: https://apidocs.lighter.xyz/docs/websocket-reference
# Rate Limits: https://apidocs.lighter.xyz/docs/rate-limits
# ═══════════════════════════════════════════════════════════════════════════════

# DEAKTIVIERT - Wir senden KEINE eigenen Pings!
# Der Server sendet uns Pings und wir antworten darauf.
LIGHTER_WS_PING_INTERVAL = None    # DEAKTIVIERT - Server sendet UNS Pings!
LIGHTER_WS_PING_TIMEOUT = None     # DEAKTIVIERT
LIGHTER_WS_PONG_TIMEOUT = 90       # Warnung wenn >90s kein Server-Ping kam

# DEAKTIVIERT - Server initiiert Ping/Pong, nicht wir!
LIGHTER_WS_PING_ON_CONNECT = False

# Lighter WebSocket Subscription Limit (from API docs: https://apidocs.lighter.xyz/docs/rate-limits)
# Lighter limits: 100 subscriptions per connection, 1000 total, 200 messages/min
# OPTION 3: We subscribe to order_book only (no trades) + market_stats/all
# This allows ALL symbols: 65 order_books + 1 market_stats = 66 < 100 ✅
# Note: market_stats/all provides prices, funding rates, OI for ALL markets
LIGHTER_WS_MAX_SYMBOLS = 99

# ═══════════════════════════════════════════════════════════════════════════════
# MAKER ORDER TIMEOUT & RETRY SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════
# Problem: POST_ONLY Orders fillen nicht → Timeouts → kein Trade Entry
# 
# LÖSUNG: Aggressivere Price Adjustments + kürzere Timeouts
# Bei Retry wird Price näher an Midprice bewegt (aggressiver = schnellerer Fill)
# ═══════════════════════════════════════════════════════════════════════════════
LIGHTER_ORDER_TIMEOUT_SECONDS = 45.0      # FIXED: Increased from 30s to reduce Ghost-Fills on illiquid pairs
MAKER_ORDER_MAX_RETRIES = 3               # 3 retries (was 2)
MAKER_ORDER_RETRY_DELAY_SECONDS = 1.0     # 1s delay (was 2s - faster iteration)
MAKER_ORDER_PRICE_ADJUSTMENT_PCT = 0.002  # 0.2% adjustment per retry (was 0.1% - more aggressive)
MAKER_ORDER_MIN_TIMEOUT_SECONDS = 15.0    # Minimum timeout (was 30s - faster for liquid pairs)
MAKER_ORDER_MAX_TIMEOUT_SECONDS = 60.0    # FIXED: Increased from 45s to allow more fill time
MAKER_ORDER_LIQUIDITY_TIMEOUT_MULTIPLIER = 0.5  # Timeout multiplier based on liquidity depth
LIGHTER_WS_MAX_SUBSCRIPTIONS = 100  # Hard limit from Lighter API

# --- Maker-to-Taker Escalation ---
# NOTE: TAKER_ESCALATION_* parameters are defined earlier (lines 131-137)
# DO NOT DUPLICATE HERE - the earlier definition is canonical

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
LIGHTER_MAX_SLIPPAGE_PCT = 0.05
LIGHTER_PRICE_EPSILON_PCT = 0.10  # 10% tolerance for shutdown close orders (was 5%, caused accidental price errors)
# NOTE: LIGHTER_ORDER_TIMEOUT_SECONDS is defined above in MAKER ORDER TIMEOUT section (45.0s)

# API Keys
LIGHTER_BASE_URL = "https://mainnet.zklighter.elliot.ai"
LIGHTER_PRIVATE_KEY = os.getenv("LIGHTER_PRIVATE_KEY")
LIGHTER_API_PRIVATE_KEY = os.getenv("LIGHTER_API_PRIVATE_KEY")
LIGHTER_ACCOUNT_INDEX = 60113
LIGHTER_API_KEY_INDEX = 3
LIGHTER_AUTO_ACCOUNT_INDEX = False
LIGHTER_MAX_API_KEY_INDEX = -1
LIGHTER_ACCOUNT_TIER = "STANDARD" # Options: "STANDARD" (1 req/s) or "PREMIUM" (50 req/s)

X10_API_BASE_URL = "https://api.starknet.extended.exchange"
X10_PRIVATE_KEY = os.getenv("X10_PRIVATE_KEY")
X10_PUBLIC_KEY = os.getenv("X10_PUBLIC_KEY")
X10_API_KEY = os.getenv("X10_API_KEY")
X10_VAULT_ID = os.getenv("X10_VAULT_ID")

TELEGRAM_ENABLED = False
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Latency Arbitrage Settings
# ═══════════════════════════════════════════════════════════════════════════════
# LATENCY ARB: Nutzt Verzögerung bei X10 Funding Rate Updates
# X10 updated Funding Rates langsamer als Lighter (~3-10s Lag typisch)
# Wenn Lighter Rate schon geändert hat, X10 aber noch nicht = Opportunity!
# ═══════════════════════════════════════════════════════════════════════════════
ENABLE_LATENCY_ARB = True         # Enable latency arbitrage (was False)
LATENCY_ARB_MIN_LAG_SECONDS = 5.0 # Minimum lag threshold (seconds)
LATENCY_ARB_MAX_LAG_SECONDS = 30.0 # Maximum lag (stale data = skip)
LATENCY_ARB_MIN_RATE_DIFF = 0.0002 # 0.02% minimum rate difference for entry

# Shutdown
CLOSE_ALL_ON_SHUTDOWN = True
SHUTDOWN_CLOSE_TIMEOUT = 60

# ═══════════════════════════════════════════════════════════════
# DUST HANDLING
# ═══════════════════════════════════════════════════════════════
DUST_THRESHOLD_MULTIPLIER = 1.1  # 10% over Min-Notional as safety buffer
DUST_ALERT_ENABLED = True        # Send Telegram alerts for dust positions

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

# Predictor Control (NEW)
USE_PREDICTOR = False                  # Master switch: False = completely disable predictor
PREDICTOR_SKIP_ON_NEGATIVE = False    # True = skip trades where predictor predicts negative (blocker)
PREDICTOR_MIN_CONFIDENCE = 0.5        # Minimum confidence threshold (was 0.7 hardcoded)

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
# 7. ORDERBOOK VALIDATION SETTINGS (Exchange-Specific)
# ==============================================================================
# These settings control when Maker orders are allowed based on orderbook state.
# Different exchanges have different orderbook characteristics via WebSocket.

# --- LIGHTER EXCHANGE (Thin WebSocket orderbooks) ---
# Lighter WS typically only streams 1-2 levels, so we use relaxed thresholds
OB_LIGHTER_MIN_DEPTH_USD = 50.0       # $50 minimum (matches typical trade size)
OB_LIGHTER_MIN_BID_LEVELS = 1         # Often only 1 level on WS stream
OB_LIGHTER_MIN_ASK_LEVELS = 1         # Often only 1 level on WS stream
OB_LIGHTER_MAX_SPREAD_PCT = 2.0       # 2% spread allowed (wider on Lighter)
OB_LIGHTER_MAX_STALENESS = 10.0       # 10 second staleness (WS can be slower)

# --- X10 EXCHANGE (Fuller orderbooks) ---
OB_X10_MIN_DEPTH_USD = 200.0          # $200 minimum depth
OB_X10_MIN_BID_LEVELS = 2             # At least 2 bid levels
OB_X10_MIN_ASK_LEVELS = 2             # At least 2 ask levels
OB_X10_MAX_SPREAD_PCT = 0.5           # 0.5% spread max
OB_X10_MAX_STALENESS = 5.0            # 5 second staleness

# --- DEFAULT PROFILE (Strict - for unknown exchanges) ---
OB_MIN_DEPTH_USD = 500.0              # Minimum depth on relevant side ($)
OB_MIN_OPPOSITE_DEPTH_USD = 200.0     # Minimum depth on opposite side ($)
OB_MIN_BID_LEVELS = 3                 # Minimum bid levels required
OB_MIN_ASK_LEVELS = 3                 # Minimum ask levels required
OB_MAX_SPREAD_PERCENT = 0.5           # Maximum spread to place Maker order (%)
OB_WARN_SPREAD_PERCENT = 0.3          # Warning threshold (%)
OB_MAX_STALENESS_SECONDS = 5.0        # Maximum orderbook age (seconds)
OB_WARN_STALENESS_SECONDS = 2.0       # Warning threshold (seconds)

# Depth Quality Thresholds (multiples of trade size)
OB_EXCELLENT_DEPTH_MULTIPLE = 10.0    # 10x trade size = excellent
OB_GOOD_DEPTH_MULTIPLE = 5.0          # 5x trade size = good
OB_MARGINAL_DEPTH_MULTIPLE = 2.0      # 2x trade size = marginal (minimum)

# --- GLOBAL SETTINGS ---
OB_FALLBACK_TO_MARKET_ORDER = True    # Use Market order if Maker conditions not met
OB_REST_FALLBACK_ENABLED = True       # Use REST API if WebSocket data stale
OB_VALIDATION_RETRY_COUNT = 2         # Number of retries before giving up
OB_VALIDATION_RETRY_DELAY = 2.0       # Seconds between retries
OB_VALIDATION_ENABLED = True          # Master switch to enable/disable validation

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
    
    env_log_file = os.getenv("BOT_LOG_FILE")
    if env_log_file:
        log_file = env_log_file
    elif per_run:
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

    # ═══════════════════════════════════════════════════════════════════════════
    # CRITICAL VALIDATION: Position Size Sanity Checks
    # ═══════════════════════════════════════════════════════════════════════════
    if DESIRED_NOTIONAL_USD < MIN_POSITION_SIZE_USD:
        _logger.error(
            f"🚨 CONFIG ERROR: DESIRED_NOTIONAL_USD (${DESIRED_NOTIONAL_USD}) < MIN_POSITION_SIZE_USD (${MIN_POSITION_SIZE_USD})\n"
            f"   → Alle Trades werden abgelehnt! Erhöhe DESIRED_NOTIONAL_USD auf mindestens ${MIN_POSITION_SIZE_USD + 10}"
        )
    
    if MAX_NOTIONAL_USD < DESIRED_NOTIONAL_USD:
        _logger.warning(f"CONFIG: MAX_NOTIONAL_USD (${MAX_NOTIONAL_USD}) < DESIRED_NOTIONAL_USD (${DESIRED_NOTIONAL_USD}) - limitiert Trade Size")
    
    # APY Filter Sanity Check
    if MIN_APY_FILTER < 0.10:
        _logger.warning(
            f"⚠️ CONFIG: MIN_APY_FILTER ({MIN_APY_FILTER*100:.1f}%) ist sehr niedrig.\n"
            f"   → Trades mit APY < 10% sind nach Fees meist unprofitabel!"
        )
    
    # Leverage Check
    estimated_exposure = DESIRED_NOTIONAL_USD * MAX_OPEN_TRADES
    estimated_capital = 200.0  # Approximate from typical balances
    estimated_leverage = estimated_exposure / estimated_capital if estimated_capital > 0 else 0
    if estimated_leverage > LEVERAGE_MULTIPLIER:
        _logger.warning(
            f"⚠️ CONFIG: Geschätzte Leverage ({estimated_leverage:.1f}x) > LEVERAGE_MULTIPLIER ({LEVERAGE_MULTIPLIER}x)\n"
            f"   → Erhöhe Kapital oder reduziere DESIRED_NOTIONAL_USD / MAX_OPEN_TRADES"
        )
    
    if LIVE_TRADING:
        missing = [k for k, v in [("X10_PRIVATE_KEY", X10_PRIVATE_KEY), ("X10_PUBLIC_KEY", X10_PUBLIC_KEY), 
                                  ("X10_API_KEY", X10_API_KEY), ("X10_VAULT_ID", X10_VAULT_ID)] if not v]
        if missing: _logger.error(f"CONFIG: Missing keys for LIVE_TRADING: {missing}")

    _logger.info("✅ CONFIG VALIDATION COMPLETED.")