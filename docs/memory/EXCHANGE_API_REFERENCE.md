# Exchange API Reference - Lighter & X10/Extended

> **Comprehensive API documentation** for funding arbitrage bot development
> Generated: 2026-01-15 | Sources: Official docs, GitHub, local external_repos

---

## Quick Reference

| Feature | Lighter | X10/Extended |
|---------|---------|--------------|
| **Base URL** | `https://mainnet.zklighter.elliot.ai` | `https://api.starknet.extended.exchange` |
| **Rate Limit (Standard)** | 60 weighted req/min | 1,000 req/min |
| **Rate Limit (Premium/MM)** | 24,000 weighted req/min | 60,000 req/5min |
| **Funding Schedule** | Hourly | Hourly |
| **Maker Fee (Standard)** | 0% | 0% |
| **Taker Fee (Standard)** | 0% | 0.025% |
| **Maker Fee (Premium)** | 0.002% (0.2 bps) | 0% |
| **Taker Fee (Premium)** | 0.02% (2 bps) | 0.025% |
| **SDK** | `lighter-python` | `x10-python-trading-starknet` |
| **Python Version** | 3.8+ | 3.10+ |

---

## LIGHTER EXCHANGE

### Authentication

```python
# API Key Index allocation:
# 0-2: Reserved (desktop, mobile PWA, mobile app)
# 3-254: Custom API keys (252 available)
# 255: Retrieves all API key info

# Headers
Authorization: Bearer <JWT_TOKEN>
# or query param for backward compat
?authorization=<TOKEN>
```

### Account Types

| Type | Maker Fee | Taker Fee | Maker Latency | Taker Latency |
|------|-----------|-----------|---------------|---------------|
| **Standard** | 0% | 0% | 200ms | 300ms |
| **Premium** | 0.002% | 0.02% | **0ms** | 150ms |

### Rate Limits (Detailed)

**Premium accounts:** 24,000 weighted requests/min
**Standard accounts:** 60 requests/min

#### Endpoint Weights

| Endpoint | Weight |
|----------|--------|
| `/api/v1/sendTx`, `/api/v1/sendTxBatch`, `/api/v1/nextNonce` | 6 |
| `/api/v1/publicPools`, `/api/v1/txFromL1TxHash` | 50 |
| `/api/v1/accountInactiveOrders`, `/api/v1/deposit/latest` | 100 |
| `/api/v1/apikeys` | 150 |
| `/api/v1/transferFeeInfo` | 500 |
| `/api/v1/trades`, `/api/v1/recentTrades` | 1200 |
| `/api/v1/changeAccountTier`, token ops, referral ops | 3000 |
| **All other endpoints** | 300 |

#### Transaction Type Limits (Standard only)

| Transaction Type | Limit |
|-----------------|-------|
| Default | 40 req/min |
| `L2Withdraw` | 2 req/min |
| `L2UpdateLeverage` | 1 req/min |
| `L2CreateSubAccount` | 2 req/min |
| `L2CreatePublicPool` | 2 req/min |
| `L2ChangePubKey` | 2 req/10s |
| `L2Transfer` | 1 req/min |

### Core Endpoints

#### Account
```
GET /api/v1/account?by=index&value={account_index}
GET /api/v1/accountsByL1Address?l1_address={address}
GET /api/v1/accountActiveOrders?account_index={idx}&market_id={id}
GET /api/v1/accountInactiveOrders?account_index={idx}&limit={n}
GET /api/v1/pnl?account_index={idx}&resolution={1m|1h|1d}
GET /api/v1/positionFunding?account_index={idx}&market_id={id}
```

#### Market Data
```
GET /api/v1/orderBooks                    # All markets metadata
GET /api/v1/orderBookDetails?market_id={id}  # Detailed market info
GET /api/v1/orderBookOrders?market_id={id}   # Live orderbook
GET /api/v1/recentTrades?market_id={id}
GET /api/v1/candles?market_id={id}&resolution={1m|1h|1d}&count_back={n}
```

#### Funding Rates
```
GET /api/v1/fundings?market_id={id}&resolution={1h|1d}
GET /api/v1/funding-rates  # Cross-exchange comparison (binance, bybit, hyperliquid, lighter)

Response:
{
  "funding_rates": [{
    "market_id": 0,
    "exchange": "lighter",
    "symbol": "BTC-PERP",
    "rate": 0.000125  // DECIMAL format, NOT percent!
  }]
}
```

#### Transactions
```
POST /api/v1/sendTx      # Single transaction
POST /api/v1/sendTxBatch # Batch transactions
GET  /api/v1/tx?by={hash|sequence_index}&value={val}
GET  /api/v1/txs?account_index={idx}&limit={n}
```

### Funding Rate Calculation

```
Premium_t = (Max(0, ImpactBid - Index) - Max(0, Index - ImpactAsk)) / Index
Premium = TWAP of 60 per-minute samples over 1 hour
FundingRate = (Premium / 8) + InterestRateComponent

// Rate capped: [-0.5%, +0.5%] per hour
// Payment: funding_payment = -position × mark_price × funding_rate
```

**Direction:**
- Positive rate → Longs pay shorts
- Negative rate → Shorts pay longs

**Payment Schedule:** Every hour on the hour

### Order Types

| Type | Description |
|------|-------------|
| `LIMIT` | Execute at specified price or better |
| `MARKET` | Execute immediately at market (with avg price limit) |
| `STOP_LOSS` | Trigger when markPrice crosses trigger, execute market |
| `STOP_LOSS_LIMIT` | Trigger at price, place limit |
| `TAKE_PROFIT` | Lock gains at target |
| `TAKE_PROFIT_LIMIT` | Lock gains with limit |
| `TWAP` | Time-weighted execution (every 30s) |

### Order Execution Options

| Option | Description |
|--------|-------------|
| **Reduce-Only** | Only reduces position toward zero |
| **Post-Only** | Cancel if would be taker (maker only) |

### Time in Force

| TIF | Behavior |
|-----|----------|
| `GOOD_TILL_TIME` | Active until expiration |
| `IMMEDIATE_OR_CANCEL` | Execute immediately or cancel (not added to book) |

### TWAP Order Details

```
Order Size = Total Amount / Number of Orders
Number of Orders = (Execution Duration / 30s) + 1

// Orders placed every 30 seconds
// Reduce-Only continues until expired, trades only when reduces position
```

### Order Matching

**Price-time priority system:**
1. Find best price on opposite side
2. If multiple orders at same price, oldest first
3. Execute at maker's price
4. SNARK proofs ensure trustless, verifiable matching

### Fat Finger Prevention

- **Ask orders:** Price must be ≥ `max(MarkPrice, bestBid) × 0.95`
- **Bid orders:** Price must be ≤ `min(MarkPrice, bestAsk) × 1.05`

### Liquidation System (Detailed)

**Margin Requirements:**
- `Initial Margin (I)` > `Maintenance Margin (M)` > `Close-out Margin (C)`
- `Account Value = Collateral + Σ(markPrice - avgEntryPrice) × position`

**Liquidation Waterfall:**

1. **Healthy**: `Account Value ≥ Initial Margin Req`
   - Full trading allowed

2. **Pre-Liquidation**: `Initial > Account ≥ Maintenance`
   - Reduce-only operations
   - Cannot increase position size
   - Health ratio must not decrease

3. **Partial Liquidation**: `Maintenance > Account > Close-out`
   - All open orders canceled
   - IoC limit orders at **zero price** sent for full position
   - Up to **1% liquidation fee** to LLP
   - Stops when above maintenance margin

4. **Full Liquidation**: `Account < Close-out`
   - LLP closes all positions
   - Takes remaining collateral

5. **Auto-Deleveraging (ADL)**:
   - When account negative and LLP can't cover
   - Positions closed against opposite-side traders
   - Selected by leverage and unrealized PnL ranking

**Zero Price Formula:**
```
zeroPrice(short) = markPrice × (1 + M × AccountValue / MaintenanceMarginReq)
zeroPrice(long) = markPrice × (1 - M × AccountValue / MaintenanceMarginReq)
```

### WebSocket

**Limits per IP:**
- Connections: 100
- Subscriptions per connection: 100
- Total subscriptions: 1000
- Max connections/min: 60
- **Max messages/min: 200** (sendTx/sendBatchTx NOT counted)
- Max inflight messages: 50
- Unique accounts: 10

**Channels:**
- `order_book` - Orderbook updates
- `market_stats` - Market statistics
- `trade` - Public trades
- `account_all` - All account updates (private)
- `account_market` - Per-market account updates (private)

### Data Structures

```go
type Order struct {
    OrderIndex           int64  `json:"i"`
    ClientOrderIndex     int64  `json:"u"`
    OwnerAccountId       int64  `json:"a"`
    InitialBaseAmount    int64  `json:"is"`
    Price                uint32 `json:"p"`
    RemainingBaseAmount  int64  `json:"rs"`
    IsAsk                uint8  `json:"ia"`
    Type                 uint8  `json:"ot"`
    TimeInForce          uint8  `json:"f"`
    ReduceOnly           uint8  `json:"ro"`
    TriggerPrice         uint32 `json:"tp"`
    Expiry               int64  `json:"e"`
    Status               uint8  `json:"st"`
    TriggerStatus        uint8  `json:"ts"`
}

type Trade struct {
    Price    uint32 `json:"p"`
    Size     int64  `json:"s"`
    TakerFee int32  `json:"tf"`
    MakerFee int32  `json:"mf"`
}
```

### Transaction Type Constants

```go
TxTypeL2ChangePubKey     = 8
TxTypeL2CreateSubAccount = 9
TxTypeL2CreatePublicPool = 10
TxTypeL2UpdatePublicPool = 11
TxTypeL2Transfer         = 12
TxTypeL2Withdraw         = 13
TxTypeL2CreateOrder      = 14
TxTypeL2CancelOrder      = 15
TxTypeL2CancelAllOrders  = 16
TxTypeL2ModifyOrder      = 17
TxTypeL2MintShares       = 18
TxTypeL2BurnShares       = 19
TxTypeL2UpdateLeverage   = 20
```

### Transaction Status

| Code | Status |
|------|--------|
| 0 | Failed |
| 1 | Pending |
| 2 | Executed |
| 3 | Pending - Final State |

### Error Codes (Key)

| Code | Error | Description |
|------|-------|-------------|
| 21506 | TooManyTxs | Too many pending txs |
| 21507 | BelowMaintenanceMargin | Can't execute transaction |
| 21508 | BelowInitialMargin | Can't execute transaction |
| 21600 | InactiveCancel | Order not active |
| 21601 | OrderBookFull | Order book full |
| 21717 | MaxOrdersPerAccount | Max active limit orders reached |
| 21733 | FatFingerPrice | Accidental price detected |
| 21734 | PriceTooFarFromMarkPrice | Limit price too far from mark |
| 23000 | TooManyRequest | Rate limited |
| 23001 | TooManySubscriptions | WS subscription limit |
| 23003 | TooManyConnections | WS connection limit |

### Python SDK

```bash
pip install git+https://github.com/elliottech/lighter-python.git
```

```python
import lighter
import asyncio

async def main():
    client = lighter.ApiClient()
    try:
        # Account API
        account_api = lighter.AccountApi(client)
        account = await account_api.account(by="index", value="1")

        # Order API
        order_api = lighter.OrderApi(client)
        orderbook = await order_api.order_book_orders(market_id=0)

        # Candlestick/Funding API
        candle_api = lighter.CandlestickApi(client)
        funding = await candle_api.fundings(
            market_id=0, resolution="1h", count_back=24
        )
    finally:
        await client.close()  # CRITICAL: Always close!

asyncio.run(main())
```

**API Classes:**
- `AccountApi` - Account info, balances, positions
- `OrderApi` - Orderbooks, trades, orders
- `TransactionApi` - Send transactions, nonces
- `CandlestickApi` - OHLCV, funding rates
- `BlockApi` - Blockchain data
- `RootApi` - Service status

---

## X10/EXTENDED EXCHANGE

### Authentication

```python
# Required headers for ALL requests:
X-Api-Key: <API_KEY>
User-Agent: <YOUR_APP_NAME>  # MANDATORY!

# Stark signatures required for:
# - Order placement/edit/cancel
# - Withdrawals
# - Transfers

# Uses SNIP12 signing standard for Starknet
```

### Rate Limits

| Tier | Limit |
|------|-------|
| Standard | 1,000 req/min |
| Market Maker | 60,000 req/5min |

**Violation:** HTTP 429

### Core Endpoints

#### Public Market Data
```
GET /api/v1/info/markets                      # All markets
GET /api/v1/info/markets/{market}/stats       # Market statistics
GET /api/v1/info/markets/{market}/orderbook   # Current orderbook
GET /api/v1/info/markets/{market}/orderbook?depth=1  # L1 only (FAST!)
GET /api/v1/info/markets/{market}/trades      # Recent trades
GET /api/v1/info/candles/{market}/{type}      # OHLCV (trades/mark/index)
GET /api/v1/info/{market}/funding             # Funding history
GET /api/v1/info/{market}/open-interests      # OI history
```

#### Private Account
```
GET /api/v1/user/account/info      # Account details
GET /api/v1/user/balance           # Balance breakdown
GET /api/v1/user/positions         # Open positions
GET /api/v1/user/positions/history # Position history
GET /api/v1/user/leverage          # Current leverage
PATCH /api/v1/user/leverage        # Update leverage
GET /api/v1/user/fees              # Fee rates
GET /api/v1/user/funding/history   # Funding payments (PAGINATED!)
```

#### Orders
```
POST   /api/v1/user/order          # Create order
PUT    /api/v1/user/order          # Edit order
DELETE /api/v1/user/order/{id}     # Cancel order
GET    /api/v1/user/orders         # Open orders
GET    /api/v1/user/orders/history # Order history
GET    /api/v1/user/trades         # Trade history
POST   /api/v1/user/order/massCancel     # Mass cancel
POST   /api/v1/user/deadmanswitch        # Auto-cancel timer
```

### Pagination (CRITICAL!)

**X10 returns max 100 records per request!**

```python
# Correct pagination pattern
all_payments = []
cursor = None
while True:
    params = {"market": symbol, "limit": 100}
    if cursor:
        params["cursor"] = cursor
    response = await session.get(url, params=params)
    data = response.json()
    payments = data.get("data", [])
    if not payments:
        break
    all_payments.extend(payments)
    cursor = data.get("pagination", {}).get("cursor")
    if not cursor:
        break
```

### Trading Fees

| Fee Type | Rate |
|----------|------|
| **Taker** | 0.025% |
| **Maker** | 0% |

### Maker Rebates

| 30-Day Market Share | Rebate |
|---------------------|--------|
| ≥ 0.5% | 0.0025% |
| ≥ 1% | 0.005% |
| ≥ 2.5% | 0.01% |
| ≥ 5% | 0.0175% |

- Auto-enrolled, no signup required
- Paid daily at 00:00 UTC
- Minimum $10 accrued to receive payout

### Funding Rate Calculation

```
AveragePremium = TWAP of 720 Premium Index samples (every 5s over 1 hour)
PremiumIndex = (Max(0, ImpactBid - Index) - Max(0, Index - ImpactAsk)) / Index
FundingRate = (AveragePremium + clamp(InterestRate - AveragePremium, -0.05%, +0.05%)) / 8

// Interest rate: 0.01% per 8-hour period
// Payment: FundingPayment = PositionSize × MarkPrice × (-FundingRate)
```

### Rate Caps by Market Group

| Group | Markets | Hourly Cap | Impact Notional |
|-------|---------|------------|-----------------|
| 1 | BTC, ETH | 1% | $10,000 |
| 2 | SOL, etc. | 2% | $5,000 |
| 3-5 | Various | 3% | $2,500 |

### Margin Schedule (Market Groups)

| Group | Markets | Max Leverage | Initial Margin | Maintenance |
|-------|---------|--------------|----------------|-------------|
| **1** | BTC-USD, ETH-USD | 50x | 2% | 1% |
| **2** | SOL-USD, etc. | 33x | 3% | 1.5% |
| **3** | Various | 25x | 4% | 2% |
| **4** | Various | 20x | 5% | 2.5% |
| **5** | Various | 10x | 10% | 5% |
| **6** | Low-liquidity | 5x | 20% | 10% |
| **EUR, XAU** | FX/Commodities | Various | Various | Various |
| **SPX, NDX** | Index futures | Various | Various | Various |

### Order Types

| Type | Description |
|------|-------------|
| `MARKET` | Execute immediately at prevailing market price |
| `LIMIT` | Execute at limit price or better |
| `CONDITIONAL` | Activates when trigger price reached (Mark/Index/Last) |
| `TWAP` | Distributes order over time at defined frequency |
| `SCALED` | Multiple limit orders across price range |

### Order Conditions

| Condition | Description |
|-----------|-------------|
| **Reduce Only** | Only reduces position, never opens opposite |
| **Post Only** | Added to book only, never executes immediately |
| **TPSL** | Take Profit/Stop Loss (always reduce-only, OCO) |

### TPSL Types

1. **Position TPSL** - Full position size, one per position
2. **Partial Position TPSL** - Up to current position size
3. **Order TPSL** - Attached to specific order

### Time in Force

| TIF | Behavior |
|-----|----------|
| `GTC` | Good Till Cancel |
| `IOC` | Immediate or Cancel |
| `FOK` | Fill or Kill (entire order or nothing) |

### Order Parameters

**Mandatory fields:**
- `price` - Required even for MARKET orders
- `fee` - Use taker fee for regular, maker for post-only
- `expiration` - Unix ms (max 90 days mainnet, 28 days testnet)
- `nonce` - ≥1, ≤2^31, must be unique
- `stark_signature` - SNIP12 signed

**Price Validation:**
- Long limit: price ≤ mark × (1 + limit_cap)
- Short limit: price ≥ mark × (1 - limit_floor)
- Market: ±5% from mark price

### Order Status Lifecycle

```
NEW → PARTIALLY_FILLED → FILLED
    → CANCELLED
    → REJECTED
    → EXPIRED

UNTRIGGERED → TRIGGERED → (follows above)
```

### Liquidation System (Detailed)

**Liquidation Criteria:**
- Margin Ratio > 100%
- `Margin Ratio = (Maintenance Margin / Equity) × 100%`
- `Equity = Wallet Balance + Unrealised PnL`

**Margin Calls:**
- First warning: Margin Ratio > 66%
- Second warning: Margin Ratio > 80%

**Liquidation Process:**

1. **XVS Liquidation** - Vault balance converted first
2. **Position Selection** - Largest unrealised loss first
3. **Partial Liquidation** - Up to 5 FOK orders at 20% each (min $1,000)
4. **Fallback** - Single FOK at bankruptcy price - 5%
5. **Insurance Fund** - Absorbs losses, earns 1% fee on profitable liquidations
6. **ADL** - If insurance fund insufficient

**Insurance Fund Limits by Group:**

| Group | Daily Access | Max Loss/Trade |
|-------|--------------|----------------|
| 1 | 15% | $100,000 |
| 2 | 15% | $50,000 |
| 3 | 15% | $50,000 |
| 4 | 10% | $25,000 |
| 5 | 5% | $15,000 |
| EUR/XAU | 15% | $50,000 |
| SPX/NDX | 10% | $25,000 |

**Global constraint:** Max 15% fund depletion per day

**Auto-Deleveraging (ADL):**
- Triggered when insurance fund can't cover
- Closes against highest-ranking opposite position
- Ranking: `PnL% × Position Margin Ratio` (profitable) or `PnL% / Position Margin Ratio` (loss)

**Price Formulas:**
```
Liquidation Price = (MM_other - WalletBalance - UPnL_other + Size × Entry) / Size
Bankruptcy Price = (MM_other/MR - WalletBalance - UPnL_other + Size × Entry) / Size
```

### WebSocket Streams

**Base URL:** `wss://api.starknet.extended.exchange`

```
/stream.extended.exchange/v1/orderbooks/{market}      # 100ms full, 10ms L1
/stream.extended.exchange/v1/publicTrades/{market}    # Real-time
/stream.extended.exchange/v1/funding/{market}         # Hourly
/stream.extended.exchange/v1/candles/{market}/{type}  # 1-10s
/stream.extended.exchange/v1/prices/mark/{market}     # Real-time
/stream.extended.exchange/v1/prices/index/{market}    # Real-time
/stream.extended.exchange/v1/account                  # Private (requires API key)
```

**Protocol:** Ping/pong every 15s, respond within 10s

### Python SDK

```bash
pip install x10-python-trading-starknet
```

```python
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.orders import OrderSide
from decimal import Decimal

# Initialize account
account = StarkPerpetualAccount(
    vault=vault_id,
    private_key="0x...",
    public_key="0x...",
    api_key="..."
)

# Create client
client = PerpetualTradingClient.create(MAINNET_CONFIG, account)

# Get positions
positions = await client.account.get_positions()

# Get balance
balance = await client.account.get_balance()

# Get funding history (with pagination!)
funding = await client.account.get_trades()  # Returns max 100!

# Place order
order = await client.orders.place_order(
    market_name="BTC-USD",
    amount_of_synthetic=Decimal("0.1"),
    price=Decimal("50000"),
    side=OrderSide.BUY
)
```

**Modules:**
- `client.account` - Balance, positions, orders, leverage
- `client.orders` - Order placement, cancellation
- `client.markets_info` - Market data

**Configurations:**
- `MAINNET_CONFIG` - Production (api.starknet.extended.exchange)
- `TESTNET_CONFIG` - Sepolia testnet (api.testnet.extended.exchange)
- `MAINNET_CONFIG_LEGACY_SIGNING_DOMAIN` - For legacy `app.x10.exchange` accounts

---

## Critical Implementation Notes

### Funding Rate Format (BOTH EXCHANGES)

```python
# ✅ CORRECT: APIs return DECIMAL format
rate_hourly = Decimal(str(api_rate))  # e.g., 0.000125 = 0.0125%

# ❌ WRONG: Don't divide by 100!
rate_hourly = Decimal(str(api_rate)) / Decimal("100")  # WRONG!

# APY calculation
apy = rate_hourly * 24 * 365
```

### Lighter account_index

```python
# 0 ≠ None! Different semantic meanings
account_index = 0     # Explicitly use account 0
account_index = None  # Use adapter's default
```

### X10 Pagination

```python
# ALWAYS paginate - max 100 per request
# Missing this loses ~37% of funding data!
```

### WebSocket Limits

| Exchange | Limit |
|----------|-------|
| Lighter | 200 messages/min (excluding sendTx) |
| X10 | Ping/pong 15s timeout, respond within 10s |

### Error Handling

```python
# Lighter
HTTP 429 → Rate limited (back off)
HTTP 400 → Invalid parameters
Error 21507 → Below maintenance margin
Error 21508 → Below initial margin
Error 21733 → Fat finger price detected

# X10
HTTP 429 → Rate limited
Error 1147 → "Open loss exceeds equity"
Error 1607 → "Withdrawals blocked"
```

---

## API Response Patterns

### Lighter Response
```json
{
  "code": 200,
  "message": "success",
  "data": { /* endpoint specific */ }
}
```

### X10 Response
```json
{
  "data": { /* endpoint specific */ },
  "pagination": {
    "cursor": "next_page_cursor",
    "count": 100
  }
}
```

---

## GitHub Repositories

| Repo | URL | Purpose |
|------|-----|---------|
| Lighter Python | `github.com/elliottech/lighter-python` | Official SDK |
| X10 Python | `github.com/x10xchange/python_sdk` (starknet branch) | Official SDK |
| Elliot Tech | `github.com/elliottech` | Lighter organization |
| X10 Exchange | `github.com/x10xchange` | X10 organization |

---

## Quick Troubleshooting

| Issue | Exchange | Solution |
|-------|----------|----------|
| Missing funding data | X10 | Implement pagination (100/page limit) |
| Rate limited | Both | Check tier, implement backoff |
| WS disconnect | Lighter | Keep <200 msg/min |
| WS timeout | X10 | Respond to ping within 10s |
| Wrong APY | Both | Don't divide rate by 100 |
| Auth failed | X10 | Include User-Agent header |
| Fat finger rejected | Lighter | Check price within 5% of mark/best |
| Order rejected | Both | Check margin requirements |
| Liquidation | Both | Monitor margin ratio, add collateral |

---

## Comparison Summary

| Aspect | Lighter | X10/Extended |
|--------|---------|--------------|
| **Architecture** | zk-SNARK proofs | Starknet (SNIP12) |
| **Funding Samples** | 60/hour (1/min) | 720/hour (1/5s) |
| **Max Funding Cap** | ±0.5%/hour | Group-dependent (1-3%) |
| **Liquidation Fee** | 1% to LLP | 1% to Insurance Fund |
| **ADL Trigger** | LLP can't cover | Insurance fund depleted |
| **Order Book Proof** | Cryptographic (trustless) | Exchange operated |
| **Maker Rebates** | No | Yes (volume-based) |
| **TWAP Interval** | 30 seconds | Configurable |

---

*Last updated: 2026-01-15*
*Sources: Official API docs, GitHub repos, external_repos documentation*
