import sys
import os
import time

# Wir müssen Python sagen, wo es den 'src' Ordner finden kann
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

try:
    from src.adapters.lighter_adapter import LighterAdapter
    from src.adapters.x10_adapter import X10Adapter
except ImportError as e:
    print(f"Fehler beim Importieren der Adapter: {e}")
    sys.exit(1)

def get_common_symbols():
    """Liest die gemeinsame Symbol-Datei."""
    try:
        with open("symbols_common.txt", "r") as f:
            symbols = f.read().splitlines()
        return symbols
    except FileNotFoundError:
        print("Fehler: 'symbols_common.txt' nicht gefunden.")
        print("Bitte führe zuerst 'get_exchange_symbols_combined.py' aus.")
        return []

def test_fetch():
    print("Starte Daten-Abruf-Test...")
    
    common_symbols = get_common_symbols()
    if not common_symbols:
        return

    # Wir nehmen das erste Symbol aus der Liste für den Test
    test_symbol = common_symbols[0]
    print(f"Teste mit gemeinsamem Symbol: {test_symbol}")

    try:
        # Initialisierung (holt auch die Symbol-Listen, aber wir brauchen die Adapter-Objekte)
        print("\n--- Initialisiere Adapter ---")
        lighter = LighterAdapter()
        time.sleep(1) # Kurze Pause
        x10 = X10Adapter()
        print("----------------------------\n")

        # --- Test Lighter ---
        print(f"--- Teste Lighter.xyz für {test_symbol} ---")
        lighter_price = lighter.fetch_mark_price(test_symbol)
        print(f"Lighter Mark Price: {lighter_price}")
        
        lighter_funding = lighter.fetch_funding_rate(test_symbol)
        print(f"Lighter Funding Rate: {lighter_funding}")
        
        # --- Test X10 ---
        print(f"\n--- Teste X10 (Extended) für {test_symbol} ---")
        
        # WICHTIG: X10 braucht das Original-Symbol (z.B. ENA-USD)
        # Unsere Adapter-Funktionen sind bereits dafür ausgelegt.
        x10_price = x10.fetch_mark_price(test_symbol)
        print(f"X10 Mark Price: {x10_price}")
        
        x10_funding = x10.fetch_funding_rate(test_symbol)
        print(f"X10 Funding Rate: {x10_funding}")

    except Exception as e:
        print(f"Ein unerwarteter Fehler ist aufgetreten: {e}")
    finally:
        print("\nTest abgeschlossen.")
        # Wir rufen close() nicht auf, da die Adapter es intern tun

if __name__ == "__main__":
    test_fetch()