import os
import sys
from dotenv import find_dotenv, load_dotenv

def mask(s, keep=4):
    if not s: return ""
    s = s.strip()
    if len(s) <= keep*2: return "*" * len(s)
    return s[:keep] + "..." + s[-keep:]

def main():
    path = find_dotenv()
    print("dotenv path:", path if path else "(nicht gefunden)")
    if path:
        load_dotenv(path)
    api_priv = os.getenv("LIGHTER_API_PRIVATE_KEY")
    eth_priv = os.getenv("LIGHTER_PRIVATE_KEY")
    print("LIGHTER_API_PRIVATE_KEY gesetzt:", bool(api_priv), mask(api_priv))
    print("LIGHTER_PRIVATE_KEY gesetzt   :", bool(eth_priv), mask(eth_priv))

if __name__ == "__main__":
    main()