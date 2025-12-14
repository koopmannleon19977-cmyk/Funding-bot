
import asyncio
import sys
import os

# Add src to path
sys.path.append(os.getcwd())

from src.adapters.lighter_client_fix import SaferSignerClient
from src.config import config

async def check_signer():
    print("Checking SignerClient methods...")
    
    # We can't instantiate it easily without keys, but we can check the class
    methods = [m for m in dir(SaferSignerClient) if not m.startswith('__')]
    print("\nMethods:")
    for m in methods:
        print(f"- {m}")

    # Inspect signatures if possible
    # from inspect import signature
    # print(signature(SaferSignerClient.create_order))

if __name__ == "__main__":
    try:
        asyncio.run(check_signer())
    except Exception as e:
        print(f"Error: {e}")
