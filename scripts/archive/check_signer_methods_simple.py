
import sys
import os

try:
    from lighter.signer_client import SignerClient
    print("Successfully imported SignerClient")
    
    methods = [m for m in dir(SignerClient) if not m.startswith('__')]
    print("\nSignerClient Methods:")
    for m in methods:
        print(f"- {m}")
        
except ImportError as e:
    print(f"ImportError: {e}")
    # try looking in venv sites
    print(f"Sys Path: {sys.path}")
