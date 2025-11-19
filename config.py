import os
import sys
import logging
import io
from dotenv import load_dotenv

load_dotenv()  # Lädt die Variablen aus der .env-Datei

# ============================================================
# 0. GEBÜHREN-KONFIGURATION (für Berechnung von TOTAL_FEES_PERCENT)
# ============================================================
TAKER_FEE_X10 = 0.00025  # 0.05% (Standard Taker Fee)
MAKER_FEE_X10 = 0.0000  # 0.02% (Standard Maker Fee)
FEES_LIGHTER = 0.0000   # 0.00% (Annahme)

# ============================================================
# 0a. MASTER-SCHALTER
# ============================================================
LIVE_TRADING = True  # ACHTUNG: True = Echte Orders werden gesendet

# Optional für spätere „Simulations"-Logik auf X10 Ebene
X10_DRY_RUN = False  # Kann später genutzt werden, um Order-Calls abzufangen

# ============================================================
# 1. POSITIONSGRÖSSEN & LIMITS (HYBRID-STRATEGIE)
# ============================================================
# Wir zielen auf eine Notional-Größe von $15 pro Trade.
DESIRED_NOTIONAL_USD = 5
MIN_POSITION_SIZE_USD = 5

# Das ist die WICHTIGE neue Sicherheitsgrenze.
# Wir erlauben, dass die Mindestgröße der Börse unsere "DESIRED"
# Größe leicht überschreitet, aber NIEMALS mehr als $20.
MAX_NOTIONAL_USD = 100

MAX_OPEN_TRADES = 5

# ============================================================
# 2. PROFIT-FILTER (EINSTIEG) - OPTIMIERT FÜR VOLUMEN
# ============================================================
# 5% APY ist ein exzellenter, risikofreier Gewinn und erhöht die Trade-Frequenz massiv.
MIN_APY_FILTER = 0.22           # Entspricht 5% APY (war 0.001 daily = ~36.5% APY)
MIN_DAILY_PROFIT_FILTER = MIN_APY_FILTER / 365  # Tägliche Rate für Kompatibilität

# === DYNAMISCHE PROFITABILITÄTS-OPTIMIERUNG ===
DYNAMIC_MIN_APY_ENABLED = True          # True = dynamisch aus DB, False = statisch
DYNAMIC_MIN_APY_MULTIPLIER = 1.1       # Mindestens 25% über dem historischen Durchschnitt
MIN_APY_FALLBACK = 1.20                 # ≈131% APY als absolute Untergrenze (sicher profitabel)

DYNAMIC_FEES_ENABLED = True             # True = Fees von API holen, False = hardcoded

# ============================================================
# 3. RISIKO-FILTER (EINSTIEG) - DEINE WAHL
# ============================================================
MAX_SPREAD_FILTER_PERCENT = 0.15   # 0.15% für höchste Qualität

# ============================================================
# 4. DYNAMISCHE SCHLIESS-LOGIK (AUSSTIEG) - OPTIMIERT FÜR KAPITAL-EFFIZIENZ
# ============================================================

# Wenn die Rate kippt, warten wir nur 8 Stunden. Länger verbrennt Geld.
FUNDING_FLIP_HOURS_THRESHOLD = 8 

# Mindest- und Maximalhaltedauer optimiert für Kapital-Effizienz
DYNAMIC_HOLD_MAX_DAYS = 7.0              # Nach 7 Tagen Kapital freimachen

# ============================================================
# 5. DYNAMISCHER STOP-LOSS & TAKE-PROFIT - OPTIMIERT FÜR EFFIZIENZ
# ============================================================
# Stop-Loss: Verlust ist 2.5x die Einstiegskosten. Fair, aber nicht zu locker.
DYNAMIC_STOP_LOSS_MULTIPLIER = 2.0
# Take-Profit: Gewinn ist 3.5x die Einstiegskosten. Sichert Gewinne schnell.
DYNAMIC_TAKE_PROFIT_MULTIPLIER = 3.0

 

# ============================================================
# 6. ORDER GUARDIAN / EXECUTION SAFETY - ERHÖHTE ROBUSTHEIT
# ============================================================
ORDER_GUARDIAN_TIMEOUT_SECONDS = 8        
ORDER_GUARDIAN_LEG2_RETRY = 1             # 1 Retry für Leg 2
ORDER_GUARDIAN_RETRY_DELAY_SECONDS = 1.0  # 1.0s Pause zwischen Retries

# ============================================================
# 7. X10 ORDER-SAFETY (Limitpreis & Slippage)
# ============================================================
X10_MAX_SLIPPAGE_PCT = 0.6        
X10_PRICE_EPSILON_PCT = 0.15      

# ============================================================
# 8. SYSTEM / PERFORMANCE & LOGGING SETUP - AGGRESSIV FÜR VOLUMEN
# ============================================================
CONCURRENT_REQUEST_LIMIT = 2
REFRESH_DELAY_SECONDS = 90       # Aggressiv: 90 Sekunden für mehr Trades
DB_FILE = "funding.db"

# --- Lighter Live-Handel & Safety ---
LIGHTER_DRY_RUN = False            
LIGHTER_MAX_SLIPPAGE_PCT = 0.6
LIGHTER_PRICE_EPSILON_PCT = 0.25
LIGHTER_ORDER_TIMEOUT_SECONDS = 8
LIGHTER_MAX_API_KEY_INDEX = -1    

# Lighter Account und API Key Indizes
LIGHTER_ACCOUNT_INDEX = 60113             
LIGHTER_API_KEY_INDEX = 3           
LIGHTER_AUTO_ACCOUNT_INDEX = False

LOG_FILE = "funding_bot.log"
LOG_LEVEL = logging.DEBUG

def setup_logging():
    """Richtet das Logging-System ein (Datei + Konsole) ohne doppelte Handler, mit UTF-8-Ausgabe auf Windows."""
    logger = logging.getLogger()
    logger.setLevel(LOG_LEVEL)

    # Bestehende Handler entfernen, um Duplikate zu vermeiden
    if logger.handlers:
        for h in list(logger.handlers):
            logger.removeHandler(h)

    log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Datei-Handler mit UTF-8
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(log_format)

    # Konsole auf UTF-8 zwingen (Windows)
    try:
        # Python 3.7+: stdout direkt reconfigurieren
        sys.stdout.reconfigure(encoding="utf-8")
        console_stream = sys.stdout
    except Exception:
        # Fallback: Wrapper erzwingt UTF-8, ersetzt nicht darstellbare Zeichen
        console_stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    console_handler = logging.StreamHandler(console_stream)
    console_handler.setFormatter(log_format)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

# ============================================================
# 9. API & KEYS (aus .env)
# ============================================================
LIGHTER_BASE_URL = "https://mainnet.zklighter.elliot.ai"
LIGHTER_PRIVATE_KEY = os.getenv("LIGHTER_PRIVATE_KEY")           
LIGHTER_API_PRIVATE_KEY = os.getenv("LIGHTER_API_PRIVATE_KEY")   

X10_PRIVATE_KEY = os.getenv("X10_PRIVATE_KEY")
X10_PUBLIC_KEY = os.getenv("X10_PUBLIC_KEY")
X10_API_KEY = os.getenv("X10_API_KEY")
X10_VAULT_ID = os.getenv("X10_VAULT_ID")

# ============================================================
# 11. VALIDIERUNG / HILFSFUNKTIONEN
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
    """
    Führt weiche Validierungen aus und gibt Warnungen aus.
    """
    _logger = logger or logging.getLogger()

    # Vault-ID prüfen
    vault_int = parse_int(X10_VAULT_ID)
    if X10_VAULT_ID and vault_int is None:
        _logger.warning(f"CONFIG WARNUNG: X10_VAULT_ID '{X10_VAULT_ID}' ist nicht numerisch. Live X10-Handel deaktiviert.")
    elif vault_int is None:
        _logger.info("CONFIG INFO: X10_VAULT_ID fehlt – X10 Live-Trading nicht verfügbar.")

    # Positionsgröße prüfen
    if DESIRED_NOTIONAL_USD < MIN_POSITION_SIZE_USD:
        _logger.warning(
            f"CONFIG WARNUNG: DESIRED_NOTIONAL_USD ({DESIRED_NOTIONAL_USD}) < MIN_POSITION_SIZE_USD "
            f"({MIN_POSITION_SIZE_USD}). Hebe sie für sinnvolle Trades an."
        )

    # Live-Trading vs. Keys
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
            _logger.error(
                f"CONFIG FEHLER: LIVE_TRADING=True aber folgende X10 Keys fehlen: {', '.join(missing)}. "
                f"Setze LIVE_TRADING=False bis die Keys korrekt sind."
            )

    # Slippage Parameter
    if X10_MAX_SLIPPAGE_PCT <= 0 or X10_MAX_SLIPPAGE_PCT > 5:
        _logger.warning(
            f"CONFIG WARNUNG: X10_MAX_SLIPPAGE_PCT={X10_MAX_SLIPPAGE_PCT} ist ungewöhnlich. Empfohlen: 0.3–1.0."
        )
    if X10_PRICE_EPSILON_PCT <= 0 or X10_PRICE_EPSILON_PCT > 5:
        _logger.warning(
            f"CONFIG WARNUNG: X10_PRICE_EPSILON_PCT={X10_PRICE_EPSILON_PCT} ist ungewöhnlich. Empfohlen: 0.1–0.5."
        )

    # Guardian Retry Logik
    if ORDER_GUARDIAN_LEG2_RETRY < 0 or ORDER_GUARDIAN_LEG2_RETRY > 3:
        _logger.warning(
            f"CONFIG WARNUNG: ORDER_GUARDIAN_LEG2_RETRY={ORDER_GUARDIAN_LEG2_RETRY} außerhalb empfohlenem Bereich (0–3)."
        )

    _logger.info("CONFIG VALIDIERUNG abgeschlossen.")

# ============================================================
# 12. INITIAL VALIDATION CALL (optional beim Import)
# ============================================================
# Du kannst validate_runtime_config(setup_logging()) direkt aufrufen,
# wenn du beim Start eine sofortige Prüfung möchtest. Sonst rufst du sie
# in deinem main-Skript nach setup_logging() auf.
#
# Beispiel im Hauptskript:
# logger = setup_logging()
# validate_runtime_config(logger)
#
# Hier NICHT automatisch aufrufen, um Seiteneffekte beim Import zu vermeiden.

ROLLBACK_DELAY_SECONDS = 3 # Sekunden Wartezeit vor einem Rollback, um Timing-Probleme zu vermeiden

# === RISIKOMANAGEMENT (das rettet dein Konto) ===
MIN_FREE_MARGIN_PCT = 0.35          # 35% frei halten → sicher bei Volatilität
MAX_EXPOSURE_PCT = 0.95              # Max 25% des Gesamtbalances in offenen Trades
MAX_VOLATILITY_PCT_24H = 8.0         # >8% 24h Change → zu riskant (ZORA, KAITO oft 50–200%)
MAX_TRADE_SIZE_USD = 500             # Hard-Cap pro Trade (auch wenn DESIRED höher)

# ==================== VOLUME FARM MODE ====================
VOLUME_FARM_MODE = True                    # Aktiviert den Farm-Modus
FARM_NOTIONAL_USD = 50                     # Kleine Positionen für Volumen
FARM_HOLD_SECONDS = 600                    # 7 Minuten halten → dann close (sicher vor Funding-Flip)
FARM_MAX_CONCURRENT = 40                   # Max 20 kleine Farm-Trades gleichzeitig
FARM_MIN_APY = 0.05                        # Mindestens ~18% APY (nicht komplett Müll)
FARM_MAX_VOLATILITY_24H = 4.0              # Nur super stabile Coins (BTC, ETH, SOL, etc.)
FARM_MAX_SPREAD_PCT = 0.15                 # Sehr enger Spread → fast kein Slippage