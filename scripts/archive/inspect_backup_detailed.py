import zipfile
import os

zip_path = r"c:\Users\koopm\funding-bot\backups\20251219_182154.zip"

try:
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        print(f"Searching for funding.db in {zip_path}")
        found = False
        for file in zip_ref.namelist():
            if "funding.db" in file:
                print(f"Found: {file}")
                found = True
        if not found:
            print("funding.db NOT found in backup.")
            
        print("\nSearching for config.py in {zip_path}")
        for file in zip_ref.namelist():
            if "config.py" in file:
                print(f"Found: {file}")

except Exception as e:
    print(f"Error: {e}")
