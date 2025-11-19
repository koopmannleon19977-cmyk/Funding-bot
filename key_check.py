import config

def check_key_format(key_name, key_value, expected_length):
    """Prüft die Länge eines Schlüssels und entfernt optional das '0x'-Präfix."""
    
    if key_value is None:
        print(f"❌ {key_name}: FEHLT im .env!")
        return
        
    original_value = key_value
    
    # 1. '0x' Präfix entfernen (falls vorhanden)
    if key_value.startswith("0x"):
        key_value = key_value[2:]
        print(f"INFO: '{key_name}' hatte '0x' Präfix. Wird entfernt.")
        
    # 2. Länge prüfen
    actual_length = len(key_value)
    
    if actual_length == expected_length:
        print(f"✅ {key_name}: LÄNGE IST KORREKT ({actual_length} Zeichen).")
        return True
    else:
        print(f"❌ {key_name}: LÄNGE IST FALSCH! Erwartet: {expected_length}, Gefunden: {actual_length}")
        if actual_length == 80 and expected_length == 40:
             print("HINWEIS: Dies ist wahrscheinlich der L1-Key, der fälschlicherweise als L2-Key verwendet wird.")
        return False

print("--- Lighter Key Formatprüfung ---")

# 1. L1/ETH Private Key (Zum Signieren der Registrierung)
check_key_format(
    "LIGHTER_PRIVATE_KEY", 
    config.LIGHTER_PRIVATE_KEY, 
    expected_length=64
)

# 2. L2 API Private Key (Wird registriert, zum Handeln)
check_key_format(
    "LIGHTER_API_PRIVATE_KEY", 
    config.LIGHTER_API_PRIVATE_KEY, 
    expected_length=40
)

print("---------------------------------")