# Orderbook & Orderbook Depth API Research

**Date**: 2026-01-15
**Purpose**: Comprehensive documentation of orderbook and depth APIs for Lighter and Extended (X10) exchanges
**Sources**: Official API docs, GitHub repositories, SDK documentation

---

## Table of Contents

1. [Lighter Exchange API](#lighter-exchange-api)
   - [REST API Endpoints](#lighter-rest-api-endpoints)
   - [WebSocket Streams](#lighter-websocket-streams)
   - [Python SDK Methods](#lighter-python-sdk-methods)
2. [Extended (X10) Exchange API](#extended-x10-exchange-api)
   - [REST API Endpoints](#extended-rest-api-endpoints)
   - [WebSocket Streams](#extended-websocket-streams)
   - [Python SDK Methods](#extended-python-sdk-methods)
3. [Comparison Table](#comparison-table)
4. [Implementation Recommendations](#implementation-recommendations)

---

## Lighter Exchange API

### Base URLs

| Environment | REST API | WebSocket |
|-------------|----------|-----------|
| **Mainnet** | `https://mainnet.zklighter.elliot.ai` | `wss://mainnet.zklighter.elliot.ai/stream` |
| **Testnet** | `https://testnet.zklighter.elliot.ai` | `wss://testnet.zklighter.elliot.ai/stream` |

### Lighter REST API Endpoints

#### 1. Get Order Books Metadata

```
GET /api/v1/orderBooks
```

**Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `market_id` | uint8 | No | 255 | Filter by specific market (255 = all) |
| `filter` | string | No | - | "all", "spot", or "perp" |

**Response Schema (OrderBook):**
```json
{
  "symbol": "ETH-PERP",
  "market_id": 0,
  "market_type": "perp",
  "base_asset_id": 1,
  "quote_asset_id": 0,
  "status": "active",
  "taker_fee": "0.0005",
  "maker_fee": "0.0001",
  "liquidation_fee": "0.005",
  "min_base_amount": "0.001",
  "min_quote_amount": "1.0",
  "supported_decimals": 8,
  "order_quote_limit": "1000000"
}
```

#### 2. Get Order Book Details

```
GET /api/v1/orderBookDetails
```

**Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `market_id` | int16 | No | 255 | Market ID (255 = all) |
| `filter` | string | No | - | "all", "spot", or "perp" |

**Response Schema (OrderBookDetail):**
```json
{
  "symbol": "ETH-PERP",
  "market_id": 0,
  "size_decimals": 4,
  "price_decimals": 2,
  "quote_multiplier": "1.0",
  "margin_fractions": {...},
  "last_trade_price": "3024.66",
  "daily_trades_count": 12500,
  "daily_volume_base": "1500.5",
  "daily_volume_quote": "4500000.0",
  "price_high_24h": "3100.00",
  "price_low_24h": "2950.00",
  "open_interest": "50000.0",
  "daily_chart": [...],
  "market_config": {...}
}
```

#### 3. Get Order Book Orders (DEPTH)

```
GET /api/v1/orderBookOrders
```

**Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `market_id` | uint8 | **Yes** | - | Market identifier |
| `limit` | int64 | **Yes** | - | **1 to 100** (pagination limit) |

**CRITICAL**: The `limit` parameter is **REQUIRED** and controls the depth of the orderbook returned.

**Response Schema (OrderBookOrders):**
```json
{
  "code": 200,
  "message": "OK",
  "total_asks": 150,
  "asks": [
    {
      "order_index": 1,
      "order_id": "abc123",
      "owner_account_index": 42,
      "initial_base_amount": "0.1",
      "remaining_base_amount": "0.08",
      "price": "3025.50",
      "order_expiry": 1640995200
    }
  ],
  "total_bids": 145,
  "bids": [
    {
      "order_index": 1,
      "order_id": "def456",
      "owner_account_index": 37,
      "initial_base_amount": "0.2",
      "remaining_base_amount": "0.2",
      "price": "3024.00",
      "order_expiry": 1640995200
    }
  ]
}
```

**Field Descriptions:**
- `total_asks/total_bids`: Total number of orders on each side (not just returned)
- `order_index`: Position in the order book
- `initial_base_amount`: Original order size
- `remaining_base_amount`: Unfilled portion
- `price`: Order price (string format)
- `order_expiry`: Unix timestamp for order expiration

#### 4. Recent Trades

```
GET /api/v1/recentTrades
```

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `market_id` | uint8 | **Yes** | Market identifier |
| `limit` | int64 | **Yes** | 1 to 100 |

#### 5. All Trades

```
GET /api/v1/trades
```

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `market_id` | uint8 | No | Filter by market |
| `account_index` | uint64 | No | Filter by account |
| `sort_by` | string | No | "block_height", "timestamp", "trade_id" |
| `limit` | int64 | Yes | 1 to 100 |

#### 6. Candlestick/OHLCV Data

```
GET /api/v1/candles
```

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `market_id` | uint8 | **Yes** | Market identifier |
| `resolution` | string | **Yes** | "1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w" |
| `start_timestamp` | int64 | **Yes** | Start time (Unix) |
| `end_timestamp` | int64 | **Yes** | End time (Unix) |
| `count_back` | int64 | **Yes** | Number of candles |

**Response Format:**
```json
[
  {
    "t": 1640995200,
    "o": "3000.00",
    "h": "3050.00",
    "l": "2980.00",
    "c": "3025.00",
    "v0": "150.5",
    "v1": "450000.0",
    "last_trade_id": "xyz789"
  }
]
```

#### 7. Funding Data

```
GET /api/v1/fundings
```

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `market_id` | uint8 | No | Market identifier |
| `resolution` | string | No | "1h" or "1d" |
| `start_timestamp` | int64 | No | Start time |
| `end_timestamp` | int64 | No | End time |
| `count_back` | int64 | No | Number of records |

**Response:**
```json
{
  "timestamp": 1640995200,
  "value": "1500.50",
  "rate": "0.0001",
  "direction": "long"
}
```

**Direction Field:**
- `direction="short"`: Shorts pay longs (long funding is NEGATIVE)
- `direction="long"`: Longs pay shorts (long funding is POSITIVE)

#### 8. Current Funding Rates

```
GET /api/v1/funding-rates
```

Returns current funding rates across multiple exchanges (binance, bybit, hyperliquid, lighter).

---

### Lighter WebSocket Streams

#### Connection

```javascript
// Mainnet
wss://mainnet.zklighter.elliot.ai/stream

// Testnet
wss://testnet.zklighter.elliot.ai/stream
```

#### Order Book Channel

**Channel**: `order_book/{MARKET_INDEX}`

**Update Frequency**: Every 50ms

**Message Format:**
```json
{
  "channel": "order_book/0",
  "type": "update",
  "timestamp": 1640995200000,
  "nonce": 12345,
  "asks": [
    {"price": "3025.50", "qty": "0.5"}
  ],
  "bids": [
    {"price": "3024.00", "qty": "0.8"}
  ]
}
```

**Important**: Use `nonce` for continuity verification. If nonce is discontinuous, re-subscribe or fetch full snapshot.

#### Market Stats Channel

**Channel**: `market_stats/{MARKET_INDEX}` or `market_stats/all`

**Data Includes:**
- Index price
- Mark price
- Funding rates
- Daily volume metrics

#### Trade Channel

**Channel**: `trade/{MARKET_INDEX}`

Broadcasts new trade executions with participant details.

#### Account Channels

- `account/{ACCOUNT_INDEX}/positions`
- `account/{ACCOUNT_INDEX}/orders`
- `account/{ACCOUNT_INDEX}/assets`
- `account/{ACCOUNT_INDEX}/stats`

---

### Lighter Python SDK Methods

**GitHub**: https://github.com/elliottech/lighter-python

#### Installation

```bash
pip install lighter-python
```

#### OrderApi Methods

```python
import lighter
import asyncio

async def main():
    client = lighter.ApiClient()
    order_api = lighter.OrderApi(client)

    try:
        # 1. Get all orderbooks metadata
        orderbooks = await order_api.order_books(
            market_id=None,  # None = all markets
            filter="perp"    # "all", "spot", or "perp"
        )

        # 2. Get detailed orderbook info
        details = await order_api.order_book_details(
            market_id=0,     # Specific market
            filter=None
        )

        # 3. Get orderbook orders (DEPTH)
        orders = await order_api.order_book_orders(
            market_id=0,     # Required
            limit=100        # Required: 1-100 (SDK allows up to 250)
        )

        print(f"Total asks: {orders.total_asks}")
        print(f"Total bids: {orders.total_bids}")
        for ask in orders.asks:
            print(f"Ask: {ask.price} x {ask.remaining_base_amount}")

    finally:
        await client.close()

asyncio.run(main())
```

**SDK Limit Constraints:**
- REST API: `limit` 1-100
- Python SDK: `limit` 1-250 (Field validation: `Field(le=250, strict=True, ge=1)`)

#### WebSocket Client

```python
import json
import logging
import lighter

logging.basicConfig(level=logging.INFO)

def on_order_book_update(market_id, order_book):
    """Callback for orderbook updates"""
    logging.info(f"Order book {market_id}:\n{json.dumps(order_book, indent=2)}")

def on_account_update(account_id, account):
    """Callback for account updates"""
    logging.info(f"Account {account_id}:\n{json.dumps(account, indent=2)}")

# Initialize WebSocket client
client = lighter.WsClient(
    order_book_ids=[0, 1],           # List of market IDs to subscribe
    account_ids=[1, 2],              # List of account IDs to monitor
    on_order_book_update=on_order_book_update,
    on_account_update=on_account_update,
)

# Run (blocking)
client.run()
```

**WsClient Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `order_book_ids` | List[int] | Market IDs to subscribe to |
| `account_ids` | List[int] | Account IDs to monitor |
| `on_order_book_update` | Callable | Callback for orderbook changes |
| `on_account_update` | Callable | Callback for account changes |

---

## Extended (X10) Exchange API

### Base URLs

| Environment | REST API | WebSocket |
|-------------|----------|-----------|
| **Mainnet** | `https://api.starknet.extended.exchange` | `wss://api.starknet.extended.exchange/stream.extended.exchange/v1` |
| **Testnet** | `https://api.testnet.extended.exchange` | `wss://api.testnet.extended.exchange/stream.extended.exchange/v1` |

### Extended REST API Endpoints

#### 1. Get Orderbook

```
GET /api/v1/info/markets/{market}/orderbook
```

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `market` | string | **Yes** | Market name (e.g., "BTC-USD") |

**Response:**
```json
{
  "status": "OK",
  "data": {
    "market": "BTC-USD",
    "bid": [
      {"qty": "0.04852", "price": "61827.7"},
      {"qty": "0.50274", "price": "61820.5"}
    ],
    "ask": [
      {"qty": "0.04852", "price": "61840.3"},
      {"qty": "0.4998", "price": "61864.1"}
    ]
  }
}
```

**Note**: This endpoint returns the FULL orderbook. For best bid/ask only, use WebSocket with `depth=1`.

#### 2. Get Markets

```
GET /api/v1/info/markets?market={market}
```

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `market` | string | No | Market name(s), repeat for multiple |

**Response includes:**
- Market name, asset details, precision
- Daily volume, price changes, highs/lows
- Current prices: last, bid, ask, mark, index
- Funding rates and open interest
- Trading configuration limits
- Starknet contract identifiers

#### 3. Get Market Statistics

```
GET /api/v1/info/markets/{market}/stats
```

**Response includes:**
- Volume and price metrics
- Bid/ask/mark/index prices
- Funding rates and schedules
- Auto-deleveraging levels by position side

#### 4. Get Trades

```
GET /api/v1/info/markets/{market}/trades
```

**Response:**
```json
{
  "status": "OK",
  "data": [
    {
      "id": 25124,
      "market": "BTC-USD",
      "side": "BUY",
      "type": "TRADE",
      "price": "61827.7",
      "qty": "0.04852",
      "timestamp": 1701563440000
    }
  ]
}
```

**Trade Types:**
- `TRADE`: Normal trade
- `LIQUIDATION`: Liquidation trade
- `DELEVERAGE`: Auto-deleverage trade

---

### Extended WebSocket Streams

#### Orderbook Stream

```
wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/{market}
```

**Query Parameters:**
| Parameter | Required | Description |
|-----------|----------|-------------|
| `market` | No | Specific market; omit for all markets |
| `depth` | No | **Set to "1" for best bid & ask only** |

**Update Frequencies:**
- **Full orderbook**: 100ms push frequency
- **Best bid & ask** (`depth=1`): **10ms push frequency**

**Message Types:**

1. **SNAPSHOT** (Full orderbook):
```json
{
  "ts": 1701563440000,
  "type": "SNAPSHOT",
  "data": {
    "m": "BTC-USD",
    "b": [{"p": "25670", "q": "0.1"}],
    "a": [{"p": "25770", "q": "0.1"}]
  },
  "seq": 1
}
```

2. **DELTA** (Changes only):
```json
{
  "ts": 1701563440050,
  "type": "DELTA",
  "data": {
    "m": "BTC-USD",
    "b": [{"p": "25671", "q": "0.05"}],
    "a": []
  },
  "seq": 2
}
```

**Field Descriptions:**
| Field | Description |
|-------|-------------|
| `type` | "SNAPSHOT" or "DELTA" |
| `ts` | Server timestamp (milliseconds) |
| `data.m` | Market name |
| `data.b[]` | Bids (descending price for snapshots) |
| `data.a[]` | Asks (ascending price for snapshots) |
| `data.b[].p` / `data.a[].p` | Price level |
| `data.b[].q` / `data.a[].q` | Size (absolute for SNAPSHOT, delta for DELTA) |
| `seq` | Sequence number for validation |

**CRITICAL**: When `q` is "0" in a DELTA message, the price level should be **removed** from the local orderbook.

#### Trades Stream

```
wss://api.starknet.extended.exchange/stream.extended.exchange/v1/publicTrades/{market}
```

**Update Frequency**: Real-time

**Message Format:**
```json
{
  "ts": 1701563440000,
  "data": [{
    "m": "BTC-USD",
    "S": "BUY",
    "tT": "TRADE",
    "T": 1701563440000,
    "p": "25670",
    "q": "0.1",
    "i": 25124
  }],
  "seq": 2
}
```

#### Funding Rates Stream

```
wss://api.starknet.extended.exchange/stream.extended.exchange/v1/funding/{market}
```

**Update Frequency**: 10ms

**Message Format:**
```json
{
  "ts": 1701563440000,
  "data": {
    "m": "BTC-USD",
    "T": 1701563440000,
    "f": "0.001"
  },
  "seq": 2
}
```

#### Candles Stream

```
wss://api.starknet.extended.exchange/stream.extended.exchange/v1/candles/{market}/{candleType}?interval={interval}
```

**Candle Types**: `trades`, `mark-prices`, `index-prices`

**Intervals**: PT1M, PT5M, PT15M, PT30M, PT1H, PT2H, PT4H, PT8H, PT12H, PT24H, P7D, P30D

#### Mark & Index Price Streams

```
wss://api.starknet.extended.exchange/stream.extended.exchange/v1/prices/mark/{market}
wss://api.starknet.extended.exchange/stream.extended.exchange/v1/prices/index/{market}
```

---

### Extended Python SDK Methods

**GitHub**: https://github.com/x10xchange/python_sdk (branch: starknet)

#### Installation

```bash
pip install x10-python-sdk
```

#### SDK Structure

```python
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.trading_client import PerpetualTradingClient

# Create account
account = StarkPerpetualAccount(
    vault=12345,
    private_key="0x...",
    public_key="0x...",
    api_key="your-api-key"
)

# Create trading client
trading_client = await PerpetualTradingClient.create(
    configuration=MAINNET_CONFIG,
    account=account
)

# Access modules
trading_client.account       # Account management
trading_client.orders        # Order management
trading_client.markets_info  # Market data (includes orderbook)
```

#### Markets Info Methods

```python
# Get orderbook snapshot
orderbook = await trading_client.markets_info.get_orderbook_snapshot(
    market_name="BTC-USD",
    limit=10  # CRITICAL: Always pass limit parameter!
)

# Response structure
print(orderbook.bids)  # List of (price, qty) tuples
print(orderbook.asks)  # List of (price, qty) tuples
```

**CRITICAL DISCOVERY**: The `get_orderbook_snapshot()` method has an optional `limit` parameter that **MUST** be explicitly passed for reliable behavior:

| Without `limit` | With `limit=N` |
|-----------------|----------------|
| Undefined behavior | Returns exactly N depth levels |
| May return empty/partial data | Maximum: 200 levels |
| Broken for low-volume symbols | Works reliably |

**Correct Usage:**
```python
# L1 (Best bid/ask only)
response = await trading_client.markets_info.get_orderbook_snapshot(
    market_name=symbol,
    limit=1  # Explicitly request L1
)

# Full depth
response = await trading_client.markets_info.get_orderbook_snapshot(
    market_name=symbol,
    limit=200  # Maximum depth
)
```

#### Account Methods

```python
# Balance and positions
balance = await trading_client.account.get_balance()
positions = await trading_client.account.get_positions()
history = await trading_client.account.get_positions_history()

# Orders
open_orders = await trading_client.account.get_open_orders()
order_history = await trading_client.account.get_orders_history()
trades = await trading_client.account.get_trades()

# Fees and leverage
fees = await trading_client.account.get_fees()
leverage = await trading_client.account.get_leverage(market_names=["BTC-USD"])
await trading_client.account.update_leverage(
    market_name="BTC-USD",
    leverage=10
)
```

---

## Comparison Table

| Feature | Lighter | Extended (X10) |
|---------|---------|----------------|
| **REST Orderbook Endpoint** | `/api/v1/orderBookOrders` | `/api/v1/info/markets/{market}/orderbook` |
| **Depth Parameter** | `limit` (1-100, required) | None (returns full book) |
| **WebSocket Orderbook** | `order_book/{MARKET_INDEX}` | `orderbooks/{market}` |
| **WS Depth Option** | No (always full) | Yes (`depth=1` for L1) |
| **WS Update Frequency** | 50ms | 100ms (full) / 10ms (L1) |
| **WS Message Types** | Updates with nonce | SNAPSHOT / DELTA |
| **SDK Orderbook Method** | `order_book_orders(market_id, limit)` | `get_orderbook_snapshot(market_name, limit)` |
| **SDK Limit Max** | 250 | 200 |
| **Rate Limit** | 60 req/min (Standard) / 24,000 req/min (Premium) | 1,000 req/min (normal) / 60,000/5min (MM) |

---

## Implementation Recommendations

### For Lighter

1. **Always pass `limit` parameter** to `order_book_orders()`:
   ```python
   orders = await order_api.order_book_orders(
       market_id=market_id,
       limit=10  # Required!
   )
   ```

2. **Use WebSocket for real-time data** - 50ms updates
3. **Track nonce** for continuity verification
4. **Premium accounts** benefit from 24,000 req/min rate limit

### For Extended (X10)

1. **ALWAYS pass `limit` parameter** to `get_orderbook_snapshot()`:
   ```python
   # CORRECT
   orderbook = await trading_client.markets_info.get_orderbook_snapshot(
       market_name=symbol,
       limit=10  # Always explicit!
   )

   # BROKEN (undefined behavior)
   orderbook = await trading_client.markets_info.get_orderbook_snapshot(
       market_name=symbol  # No limit = may return empty!
   )
   ```

2. **Use `depth=1` for L1 WebSocket** - 10ms updates (10x faster)
3. **Handle DELTA messages correctly** - `q=0` means remove level
4. **Track `seq` number** for message ordering

### Common Pitfalls

1. **Missing `limit` parameter** - Both SDKs may return incomplete data
2. **Treating cumulative fields as absolute** - Always compute deltas
3. **Not handling one-sided depth** - Low-volume symbols may have bids OR asks, not both
4. **Ignoring sequence numbers** - Can lead to stale orderbook state

---

## Rate Limits Summary

### Lighter

| Account Type | Rate Limit |
|--------------|------------|
| Standard | 60 weighted requests/minute |
| Premium | 24,000 weighted requests/minute |

### Extended (X10)

| Account Type | Rate Limit |
|--------------|------------|
| Normal | 1,000 requests/minute per IP |
| Market Maker | 60,000 requests/5 minutes |
| WebSocket | No rate limits |

---

## GitHub Repositories

### Elliot Technologies (Lighter)

| Repository | Description | Stars |
|------------|-------------|-------|
| [lighter-python](https://github.com/elliottech/lighter-python) | Public Python SDK | 288 |
| [lighter-go](https://github.com/elliottech/lighter-go) | Public Go SDK | 69 |
| [lighter-contracts](https://github.com/elliottech/lighter-contracts) | Smart contracts | 1 |
| [lighter-prover](https://github.com/elliottech/lighter-prover) | ZK prover | 57 |

### X10 Exchange

| Repository | Description | Stars |
|------------|-------------|-------|
| [python_sdk](https://github.com/x10xchange/python_sdk) | Python SDK for X10 | 31 |
| [extended-sdk-golang](https://github.com/x10xchange/extended-sdk-golang) | Go SDK | 3 |
| [examples](https://github.com/x10xchange/examples) | API examples (TypeScript) | 3 |

---

## Authentication

### Lighter

Most endpoints support optional `auth` parameter. WebSocket requires auth token:

```python
token = client.create_auth_token_with_expiry()
```

### Extended (X10)

Required headers for authenticated requests:
- `X-Api-Key`: Your API key
- `User-Agent`: Required for both REST and WebSocket

---

**Document Version**: 1.0
**Last Updated**: 2026-01-15
**Author**: Claude Code Research Agent
