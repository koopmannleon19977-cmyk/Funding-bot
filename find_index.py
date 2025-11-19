import requests
import json
import config 
import sys
import os

# =========================================================
# KONFIGURATION
# =========================================================
# L1-Adresse aus der letzten Log-Ausgabe:
L1_ADDRESS = "0xA9BDb8a97805f81203781556bdB0705444319Ab7" 

# KORREKTUR: Verwende die vollständige, feste Base URL, um den 'No host supplied'-Fehler zu vermeiden.
BASE_URL = "https://mainnet.zklighter.elliot.ai"
API_ENDPOINT = "/api/v1/accountsByL1Address"
# =========================================================

def find_account_index():
    """Ruft die Account-Informationen basierend auf der L1-Adresse ab."""
    
    # KORREKTUR: Parametername von l1Address zu l1_address geändert
    full_url = f"{BASE_URL}{API_ENDPOINT}?l1_address={L1_ADDRESS}"
    
    print(f"Sende Anfrage an: {full_url}")

    headers = {"accept": "application/json"}

    try:
        # Führe die Anfrage aus
        response = requests.get(full_url, headers=headers, timeout=10)
        response.raise_for_status()  # Löst Exception bei 4xx/5xx Fehlern aus
        
        data = response.json()
        
        if not data or not data.get('accounts'):
            print("\n🚨 ERGEBNIS: KEIN KONTO GEFUNDEN.")
            print("Stellen Sie sicher, dass Ihr L1-Schlüssel korrekt ist und das Konto Guthaben hat.")
            return

        print("\n✅ KONTO-INFORMATIONEN GEFUNDEN:")
        
        # Gehe alle gefundenen Konten durch (sollte nur eines sein)
        for account in data['accounts']:
            account_index = account.get('accountIndex')
            
            print("-" * 50)
            print(f"Definitiver Account Index (für config.py): {account_index}")
            print(f"L1 Address:    {account.get('l1Address')}")
            
            api_keys_info = account.get('apiKeys')
            if api_keys_info:
                 print(f"Registrierte API Key Indizes: {[k['index'] for k in api_keys_info]}")
                 print(f"AKTION: Verwenden Sie diesen Account Index ({account_index}) und EINEN der gefundenen API Key Indizes in config.py.")
            else:
                 print("API Key Indizes: KEINE VORHANDEN. Sie müssen Index 0 oder 1 verwenden.")
        
        print("-" * 50)
        
    except requests.exceptions.HTTPError as e:
        print(f"\nFEHLER (HTTP): Die API lehnte die Anfrage ab (Code {response.status_code}).")
        print(f"Antwort: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"\nFEHLER (Netzwerk): Die Anfrage konnte nicht gesendet werden: {e}")
    except Exception as e:
        print(f"\nUNERWARTETER FEHLER: {e}")

if __name__ == "__main__":
    find_account_index()