import asyncio
import logging
import sys
import os

# FÜGEN SIE DEN WURZELPFAD ZU PYTHON'S SUCHPFAD HINZU
# Dies stellt sicher, dass Python config und src/adapters finden kann.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Importiere config aus dem Hauptverzeichnis
import config
# Importiere die Adapter aus dem src.adapters-Paket (passt zur Pfadstruktur)
from src.adapters.x10_adapter import X10Adapter
from src.adapters.lighter_adapter import LighterAdapter


# ============================================================
# ARB EXECUTIONER (Ihre GUARDIAN-Logik)
# ============================================================

class ArbExecutioner:
    """Koordiniert die atomare Ausführung eines Delta-neutralen Arbitrage-Trades."""
    def __init__(self, x10_adapter: X10Adapter, lighter_adapter: LighterAdapter):
        self.x10 = x10_adapter
        self.lighter = lighter_adapter
        self.timeout = config.ORDER_GUARDIAN_TIMEOUT_SECONDS
        # Nutzt den neuen Wert von 50 USD
        self.notional_usd = config.POSITION_SIZE_USD 
        logging.info(f"Guardian initialisiert. Timeout pro Leg: {self.timeout}s.")

    async def execute_atomic_trade(self, symbol: str, leg1_side: str, leg2_side: str) -> bool:
        """
        Führt den Trade mit Guardian-Logik aus.
        Leg 1: X10
        Leg 2: Lighter
        """
        if not config.LIVE_TRADING:
            logging.warning("LIVE_TRADING ist FALSE. DRY-RUN Simulation läuft.")
            return True 

        # --- 1. LEG 1: Order auf X10 ---
        logging.info(f"GUARDIAN: Starte Leg 1 (X10 {leg1_side} {symbol}) mit {self.notional_usd} USD.")
        
        try:
            leg1_ok = await self.x10.open_live_position(symbol, leg1_side, self.notional_usd)
        except Exception as e:
            logging.error(f"GUARDIAN FEHLER: Leg 1 (X10) fehlgeschlagen (Exception): {e}.")
            return False

        if not leg1_ok:
            logging.error("GUARDIAN FEHLER: Leg 1 (X10) Order konnte nicht platziert werden. Trade abgebrochen.")
            return False

        logging.info("GUARDIAN: Leg 1 (X10) erfolgreich ausgeführt. Fahre mit Leg 2 fort.")

        # --- 2. LEG 2: Order auf Lighter mit Timeout-Guardian ---
        logging.info(f"GUARDIAN: Starte Leg 2 (Lighter {leg2_side} {symbol}) mit {self.notional_usd} USD.")
        
        leg2_ok = False
        try:
            leg2_ok = await asyncio.wait_for(
                self.lighter.open_live_position(symbol, leg2_side, self.notional_usd),
                timeout=self.timeout
            )
        except asyncio.TimeoutError:
            logging.error(f"GUARDIAN FEHLER: Leg 2 (Lighter) Timeout nach {self.timeout}s erreicht.")
            leg2_ok = False
        except Exception as e:
            logging.error(f"GUARDIAN FEHLER: Leg 2 (Lighter) fehlgeschlagen (Exception): {e}.")
            leg2_ok = False

        # --- 3. PRÜFUNG & ROLLBACK ---
        if leg2_ok:
            logging.info("GUARDIAN ERFOLG: Beide Legs erfolgreich ausgeführt. Position ist Delta-neutral.")
            return True
        else:
            # ROLLBACK-Szenario: Leg 2 fehlgeschlagen. Leg 1 MUSS geschlossen werden.
            logging.critical("GUARDIAN ROLLBACK INITIERT: Leg 2 Fehler! Schließe jetzt Leg 1 auf X10.")
            
            rollback_side = "SELL" if leg1_side.upper() == "BUY" else "BUY"

            rollback_ok = await self.x10.close_live_position(symbol, rollback_side, self.notional_usd)

            if rollback_ok:
                logging.info("GUARDIAN ROLLBACK ERFOLGREICH: X10 Leg 1 wurde geschlossen. Position ist sicher.")
            else:
                logging.error("GUARDIAN ROLLBACK FEHLGESCHLAGEN: Manuelle Intervention nötig! X10 Leg 1 ist offen.")
                
            return False


# ============================================================
# HAUPT-EINSTIEGSPUNKT
# ============================================================

async def main():
    """Startet den Bot, lädt Caches und versucht einen Trade (Beispiel)."""
    
    # 1. Logging initialisieren
    logger = config.setup_logging()
    logger.info("Bot-System wird gestartet...")
    
    # 2. Konfiguration validieren
    config.validate_runtime_config(logger)

    # 3. Adapter initialisieren
    x10_adapter = X10Adapter()
    lighter_adapter = LighterAdapter()
    executor = ArbExecutioner(x10_adapter, lighter_adapter)
    
    # 4. Markt-Caches laden
    logger.info("Lade Markt-Caches (Preise, Funding Rates)...")
    await asyncio.gather(
        x10_adapter.load_market_cache(), 
        lighter_adapter.load_market_cache()
    )
    logger.info("Markt-Caches geladen. Starte Handelslogik...")

    # *************************************************************
    # HIER MUSS IHRE SCAN-LOGIK ZUR FINDUNG DES ARB-SIGNALS EINFÜGEN!
    # *************************************************************
    
    # Beispiel-Trade, wenn ein Arbitrage-Signal gefunden wird:
    SYMBOL = "BTC-USD" 
    X10_SIDE = "BUY"
    LIGHTER_SIDE = "SELL"

    logger.info(f"Arbitrage-Signal gefunden: X10={X10_SIDE}, Lighter={LIGHTER_SIDE} für {SYMBOL}")
    trade_succeeded = await executor.execute_atomic_trade(
        symbol=SYMBOL, 
        leg1_side=X10_SIDE, 
        leg2_side=LIGHTER_SIDE
    )

    if trade_succeeded:
        logger.info(f"✅ Handelsauftrag für {SYMBOL} erfolgreich und atomar ausgeführt!")
    else:
        logger.error(f"❌ Handelsauftrag für {SYMBOL} fehlgeschlagen. Rollback-Status im Log prüfen!")

    # 5. Adapter schließen (aclose ist jetzt für beide Adapter async)
    await x10_adapter.aclose()
    await lighter_adapter.aclose()
    logger.info("Bot-Lauf beendet.")

if __name__ == "__main__":
    try:
        # Führe die asynchrone Hauptfunktion aus
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown durch Benutzer.")
        sys.exit(0)