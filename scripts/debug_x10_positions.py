
import asyncio
import os
import sys
import logging

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from src.adapters.x10_adapter import X10Adapter

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    print("Initializing X10 Adapter...")
    adapter = X10Adapter()
    
    print("Fetching open positions...")
    try:
        client = await adapter._get_auth_client()
        resp = await client.account.get_positions()
        
        if resp and resp.data:
            with open('debug_result.log', 'w', encoding='utf-8') as f:
                f.write(f"Found {len(resp.data)} positions:\n")
                for p in resp.data:
                    f.write(f"SYM: {getattr(p, 'market', 'UNKNOWN')}\n")
                    f.write(f"SIZE: {getattr(p, 'size', 'N/A')}\n")
                    f.write(f"SIDE: {getattr(p, 'side', 'N/A')}\n")
                    f.write("---\n")
        else:
            with open('debug_result.log', 'w', encoding='utf-8') as f:
                f.write("No positions found.\n")
            
    except Exception as e:
        with open('debug_result.log', 'w', encoding='utf-8') as f:
            f.write(f"Error: {e}\n")

if __name__ == "__main__":
    asyncio.run(main())
