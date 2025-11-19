import sys
import os

# Wir müssen Python sagen, wo es den 'src' Ordner finden kann,
# damit wir die Adapter importieren können.
# sys.path.append fügt einen Ordner zum Suchpfad hinzu.
# os.path.abspath(os.path.join(os.path.dirname(__file__), '..')) 
#   -> Geht vom aktuellen Script ('.../scripts/') einen Ordner hoch ('..')
#   -> Das Ergebnis ist der Hauptordner 'C:\...\funding-bot'
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

try:
    print("Import-Versuch 1: LighterAdapter...")
    from src.adapters.lighter_adapter import LighterAdapter
    print("...LighterAdapter-Import erfolgreich.")
    
    print("Import-Versuch 2: X10Adapter...")
    from src.adapters.x10_adapter import X10Adapter
    print("...X10Adapter-Import erfolgreich.")
    
except ImportError as e: # 'e' ist die genaue Fehlermeldung
    print(f"\n--- ECHTER IMPORT-FEHLER ---")
    print(f"Fehlerdetails: {e}")
    print(f"------------------------------\n")
    print(f"Aktueller Suchpfad (sys.path): {sys.path}")
    sys.exit(1) # Beendet das Script mit einem Fehlercode

def compare_symbols():
    print("Starte Symbol-Vergleich...")
    
    # 1. Adapter initialisieren (das lädt die Symbole im Cache)
    try:
        lighter = LighterAdapter()
        x10 = X10Adapter()
    except Exception as e:
        print(f"Fehler bei der Initialisierung der Adapter: {e}")
        return

    # 2. Symbol-Listen holen
    lighter_symbols = set(lighter.list_symbols())
    x10_symbols = set(x10.list_symbols())

    print(f"\nLighter: {len(lighter_symbols)} Symbole gefunden.")
    print(f"X10: {len(x10_symbols)} Symbole gefunden.")

    # 3. Symbole vergleichen
    common_symbols = sorted(list(lighter_symbols.intersection(x10_symbols)))
    only_lighter = sorted(list(lighter_symbols.difference(x10_symbols)))
    only_x10 = sorted(list(x10_symbols.difference(lighter_symbols)))

    print(f"\n--- Ergebnisse ---")
    print(f"Gemeinsame Symbole: {len(common_symbols)}")
    print(f"Nur auf Lighter: {len(only_lighter)}")
    print(f"Nur auf X10: {len(only_x10)}")

    # 4. Ergebnisse in Dateien schreiben
    try:
        with open("symbols_common.txt", "w") as f:
            f.write("\n".join(common_symbols))
        
        with open("symbols_lighter_only.txt", "w") as f:
            f.write("\n".join(only_lighter))

        with open("symbols_x10_only.txt", "w") as f:
            f.write("\n".join(only_x10))
            
        # Erstellt eine Diff-Datei für den schnellen Überblick
        with open("symbols_diff.txt", "w") as f:
            f.write(f"===== Gemeinsame Symbole ({len(common_symbols)}) =====\n")
            f.write("\n".join(common_symbols))
            f.write(f"\n\n===== Nur auf Lighter ({len(only_lighter)}) =====\n")
            f.write("\n".join(only_lighter))
            f.write(f"\n\n===== Nur auf X10 ({len(only_x10)}) =====\n")
            f.write("\n".join(only_x10))
            
        print("\nErfolgreich 4 Dateien geschrieben:")
        print("- symbols_common.txt")
        print("- symbols_lighter_only.txt")
        print("- symbols_x10_only.txt")
        print("- symbols_diff.txt")

    except Exception as e:
        print(f"Fehler beim Schreiben der Dateien: {e}")

    # 5. Adapter schließen (falls nötig)
    lighter.close()
    x10.close()
    print("\nVergleich abgeschlossen.")

# Dieser Teil sorgt dafür, dass die Funktion 'compare_symbols'
# nur ausgeführt wird, wenn wir das Script direkt starten.
if __name__ == "__main__":
    compare_symbols()