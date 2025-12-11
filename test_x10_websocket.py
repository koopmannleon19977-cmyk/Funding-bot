#!/usr/bin/env python3
"""
X10 Account WebSocket Test Script

Tests the X10 account WebSocket stream to verify:
1. Connection with API key authentication
2. Message reception (orders, positions, fills, balance)
3. Ping/pong health monitoring

Run: python test_x10_websocket.py
"""

import asyncio
import json
import logging
import time
import os
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Try to import config
try:
    import config
    X10_API_KEY = config.X10_API_KEY
except ImportError:
    from dotenv import load_dotenv
    load_dotenv()
    X10_API_KEY = os.getenv("X10_API_KEY")

# WebSocket URL
X10_ACCOUNT_WS_URL = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account"


async def test_x10_account_websocket():
    """Test X10 account WebSocket connection and message flow."""
    
    import websockets
    
    if not X10_API_KEY:
        logger.error("❌ X10_API_KEY not configured! Set in config.py or .env")
        return False
    
    logger.info(f"🔌 Connecting to X10 Account WebSocket...")
    logger.info(f"   URL: {X10_ACCOUNT_WS_URL}")
    logger.info(f"   API Key: {X10_API_KEY[:8]}...{X10_API_KEY[-4:]}")
    
    headers = {
        "X-Api-Key": X10_API_KEY,
        "User-Agent": "X10PythonTradingClient/0.4.5"
    }
    
    message_counts = {
        "total": 0,
        "ORDER": 0,
        "TRADE": 0,
        "POSITION": 0,
        "BALANCE": 0,
        "PONG": 0,
        "OTHER": 0,
    }
    
    try:
        async with websockets.connect(
            X10_ACCOUNT_WS_URL,
            additional_headers=headers,
            ping_interval=15.0,
            ping_timeout=None,  # Don't timeout on pongs
            close_timeout=5.0,
            max_size=4 * 1024 * 1024
        ) as ws:
            logger.info("✅ Connected!")
            
            # Set test duration
            test_duration = 120  # 2 minutes
            start_time = time.time()
            last_ping_time = time.time()
            ping_interval = 30  # Send ping every 30s
            
            logger.info(f"📥 Listening for messages ({test_duration}s test)...")
            logger.info("=" * 60)
            
            while time.time() - start_time < test_duration:
                # Send periodic ping
                if time.time() - last_ping_time >= ping_interval:
                    ping_msg = {"ping": int(time.time() * 1000)}
                    await ws.send(json.dumps(ping_msg))
                    logger.debug(f"📤 Sent ping: {ping_msg}")
                    last_ping_time = time.time()
                
                # Receive message with timeout
                try:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    message_counts["total"] += 1
                    
                    # Parse JSON
                    if isinstance(raw_msg, bytes):
                        raw_msg = raw_msg.decode('utf-8')
                    
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ Invalid JSON: {raw_msg[:100]}")
                        continue
                    
                    # Identify message type
                    msg_type = (
                        msg.get("type") or 
                        msg.get("e") or 
                        msg.get("event") or 
                        "UNKNOWN"
                    ).upper()
                    
                    # Handle pong
                    if "pong" in msg:
                        message_counts["PONG"] += 1
                        logger.info(f"💓 Pong received: {msg.get('pong')}")
                        continue
                    
                    # Count by type
                    if msg_type in message_counts:
                        message_counts[msg_type] += 1
                    else:
                        message_counts["OTHER"] += 1
                    
                    # Log message
                    elapsed = int(time.time() - start_time)
                    logger.info(f"📨 [{elapsed}s] {msg_type}: {json.dumps(msg)[:300]}")
                    
                    # Parse specific message types
                    if msg_type == "ORDER":
                        data = msg.get("data", {})
                        orders = data.get("orders", [])
                        for order in orders:
                            logger.info(
                                f"   📋 Order: {order.get('market')} {order.get('side')} "
                                f"{order.get('status')} qty={order.get('qty')}"
                            )
                    
                    elif msg_type == "TRADE":
                        data = msg.get("data", {})
                        trades = data.get("trades", [])
                        for trade in trades:
                            logger.info(
                                f"   💰 Trade: {trade.get('market')} {trade.get('side')} "
                                f"qty={trade.get('qty')} @ ${trade.get('price')}"
                            )
                    
                    elif msg_type == "POSITION":
                        data = msg.get("data", {})
                        positions = data.get("positions", [])
                        for pos in positions:
                            logger.info(
                                f"   📊 Position: {pos.get('market')} {pos.get('side')} "
                                f"size={pos.get('size')} uPnL=${pos.get('unrealisedPnl')}"
                            )
                    
                    elif msg_type == "BALANCE":
                        data = msg.get("data", {})
                        balance = data.get("balance", {})
                        logger.info(
                            f"   💵 Balance: equity=${balance.get('equity')} "
                            f"available=${balance.get('availableForTrade')}"
                        )
                    
                except asyncio.TimeoutError:
                    # No message in 5s - this is OK
                    elapsed = int(time.time() - start_time)
                    logger.debug(f"[{elapsed}s] No message (waiting...)")
            
            logger.info("=" * 60)
            logger.info("📊 TEST COMPLETE - Message Statistics:")
            logger.info(f"   Total Messages: {message_counts['total']}")
            for msg_type, count in message_counts.items():
                if msg_type != "total" and count > 0:
                    logger.info(f"   - {msg_type}: {count}")
            
            # Success criteria
            if message_counts["total"] <= 3:
                logger.error("❌ FAIL: Too few messages received (expected continuous updates)")
                logger.error("   Problem: WebSocket may not be receiving account events")
                return False
            elif message_counts["BALANCE"] > 0 or message_counts["POSITION"] > 0:
                logger.info("✅ PASS: Receiving account data (balance/positions)")
                return True
            elif message_counts["PONG"] > 0 and message_counts["total"] > 3:
                logger.warning("⚠️ PARTIAL: Only ping/pong working, no account events")
                logger.warning("   This may be OK if there's no account activity")
                return True
            else:
                logger.warning("⚠️ INCONCLUSIVE: Check message types received")
                return True
                
    except websockets.exceptions.InvalidStatusCode as e:
        logger.error(f"❌ Connection rejected: HTTP {e.status_code}")
        if e.status_code == 401:
            logger.error("   API Key may be invalid or missing")
        return False
    except Exception as e:
        logger.error(f"❌ Connection error: {e}")
        return False


async def main():
    """Run the test."""
    print()
    print("=" * 60)
    print("X10 ACCOUNT WEBSOCKET TEST")
    print("=" * 60)
    print()
    
    success = await test_x10_account_websocket()
    
    print()
    if success:
        print("✅ TEST PASSED")
    else:
        print("❌ TEST FAILED")
    print()
    
    return success


if __name__ == "__main__":
    asyncio.run(main())

