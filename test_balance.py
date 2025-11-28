# test_balance.py
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def test():
    import aiohttp
    
    api_key = os. getenv("X10_API_KEY")
    print(f"API Key loaded: {api_key[:10]}..." if api_key else "NO API KEY!")
    
    if not api_key:
        return
    
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.starknet.extended.exchange/api/v1/user/balance",
            headers=headers
        ) as resp:
            print(f"Status: {resp.status}")
            data = await resp.json()
            print(f"Response: {data}")

asyncio.run(test())