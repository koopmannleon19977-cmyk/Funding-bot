import asyncio
import ssl

import aiohttp
import certifi


async def test_rest(symbol):
    url = f"https://api.starknet.extended.exchange/api/v1/info/markets/{symbol}/orderbook"
    print(f"Testing {url}")
    try:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession() as session, session.get(url, ssl=ssl_ctx, timeout=10) as resp:
            print(f"Status: {resp.status}")
            if resp.status == 200:
                data = await resp.json()
                print("Success! Data keys:", data.keys())
                if "data" in data:
                    bid_count = len(data["data"].get("bid", []))
                    ask_count = len(data["data"].get("ask", []))
                    print(f"Bids: {bid_count}, Asks: {ask_count}")
                    if bid_count > 0:
                        print(f"Sample Bid: {data['data'].get('bid')[0]}")
                else:
                    print("No 'data' in response")
            else:
                print("Failed response:", await resp.text())
    except Exception as e:
        print(f"Error: {e}")


async def main():
    await test_rest("ETC-USD")
    await test_rest("USDJPY-USD")
    await test_rest("BTC-USD")  # Control


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
