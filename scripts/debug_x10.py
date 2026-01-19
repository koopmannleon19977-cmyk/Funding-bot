import os
import sys

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json
import logging
import ssl

import aiohttp
import certifi

# Setup Logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def test_x10_global_stream():
    """Test X10 Global Orderbook Stream"""
    print("\n--- TESTING X10 GLOBAL STREAM ---")
    stream_url = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1"

    # Manually connect to /orderbooks without market
    url = f"{stream_url}/orderbooks"
    print(f"Connecting to: {url}")

    import websockets

    try:
        async with websockets.connect(url, ssl=ssl.create_default_context(cafile=certifi.where())) as ws:
            print("Connected!")
            # Wait for first message
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                print(f"Received message: {str(msg)[:200]}...")
                data = json.loads(msg)
                print(f"Message type: {data.get('type')}")
                if data.get("data"):
                    print(f"Sample Data: {str(data.get('data'))[:100]}")
            except TimeoutError:
                print("TIMEOUT waiting for message!")
    except Exception as e:
        print(f"CONNECTION ERROR: {e}")


async def test_x10_rest_fallback(symbol="ETC-USD"):
    """Test X10 REST Fallback"""
    print(f"\n--- TESTING X10 REST FOR {symbol} ---")
    url = f"https://api.starknet.extended.exchange/api/v1/info/markets/{symbol}/orderbook"
    print(f"GET {url}")

    async with aiohttp.ClientSession() as session, session.get(url, timeout=5) as resp:
        print(f"Status: {resp.status}")
        txt = await resp.text()
        print(f"Body: {txt[:500]}")


async def main():
    await test_x10_global_stream()
    await test_x10_rest_fallback("ETC-USD")
    await test_x10_rest_fallback("USDJPY-USD")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
