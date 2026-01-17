# Technische Wissensdatenbank: Lighter.xyz & Extended Exchange

> **Generiert**: 2025-01-17
> **Forschungstiefe**: Exhaustive
> **Strategie**: Unified

---

## Executive Summary

Diese Wissensdatenbank dokumentiert alle technischen Details der beiden dezentralen Perpetual Futures Börsen **Lighter.xyz** und **Extended Exchange** (ehemals X10). Beide Börsen bieten hochperformante CLOB-basierte Trading-Infrastrukturen mit Layer-2-Settlement, jedoch mit unterschiedlichen technologischen Ansätzen:

| Aspekt | Lighter.xyz | Extended Exchange |
|--------|-------------|-------------------|
| **Layer 2** | Custom ZK-Rollup (Plonky2) | StarkEx → Starknet Migration |
| **Settlement** | Ethereum | Starknet |
| **Proof-System** | SNARK (Plonky2) | STARK (StarkEx/Cairo) |
| **Funding-Intervall** | 1 Stunde | 1 Stunde |
| **Account-Typen** | Standard / Premium | Einheitlich |

---

## Teil 1: Lighter.xyz Ökosystem

### 1.1 Architektur-Übersicht

Lighter ist eine dezentrale Perpetual Futures Börse auf einer **app-spezifischen ZK Layer-2** auf Ethereum. Die Plattform verwendet Custom ZK-Circuits (Plonky2) für:

- **Verifiable Order Matching**: Price-Time-Priority wird kryptografisch bewiesen
- **Liquidations**: ZK-Beweise garantieren faire Liquidationen
- **Account Updates**: Alle Zustandsänderungen sind verifizierbar

**Kernkomponenten**:
```
┌─────────────────────────────────────────────────────────┐
│                    Lighter Protocol                      │
├─────────────────────────────────────────────────────────┤
│  [Sequencer] → [Matching Engine] → [Prover] → [L1 Verifier]  │
│       │              │                │             │         │
│   Order Queue    ZK Circuits      Plonky2       Ethereum      │
└─────────────────────────────────────────────────────────┘
```

### 1.2 API-Endpunkte

#### REST API Base URLs
- **Mainnet**: `https://mainnet.zklighter.elliot.ai`
- **Testnet**: `https://testnet.zklighter.elliot.ai`
- **Explorer**: `https://explorer.elliot.ai`

#### WebSocket Endpoints
- **Mainnet**: `wss://mainnet.zklighter.elliot.ai/stream`
- **Testnet**: `wss://testnet.zklighter.elliot.ai/stream`

#### Wichtige REST Endpoints

| Endpoint | Methode | Beschreibung | Gewichtung |
|----------|---------|--------------|------------|
| `/api/v1/sendTx` | POST | Transaktion senden | 6 |
| `/api/v1/sendTxBatch` | POST | Batch-Transaktionen (max 50) | 6 |
| `/api/v1/nextNonce` | GET | Nächste Nonce für API-Key | 6 |
| `/api/v1/account` | GET | Account-Daten | 300 |
| `/api/v1/orderBooks` | GET | Orderbuch-Daten | 300 |
| `/api/v1/trades` | GET | Trade-Historie | 600 |
| `/api/v1/recentTrades` | GET | Aktuelle Trades | 600 |
| `/api/v1/positions` | GET | Positionen | 300 |
| `/api/v1/changeAccountTier` | POST | Account-Typ ändern | 3000 |

### 1.3 WebSocket Channels

```json
// Order Book Stream
{"type": "subscribe", "channel": "order_book/{MARKET_INDEX}"}

// Market Stats
{"type": "subscribe", "channel": "market_stats/{MARKET_INDEX}"}
{"type": "subscribe", "channel": "market_stats/all"}

// Trade Stream
{"type": "subscribe", "channel": "trade/{MARKET_INDEX}"}

// Account Channels (Auth erforderlich)
{"type": "subscribe", "channel": "account_all/{ACCOUNT_ID}"}
{"type": "subscribe", "channel": "account_market/{MARKET_ID}/{ACCOUNT_ID}", "auth": "{AUTH_TOKEN}"}
{"type": "subscribe", "channel": "account_orders/{MARKET_INDEX}/{ACCOUNT_ID}", "auth": "{AUTH_TOKEN}"}
{"type": "subscribe", "channel": "account_all_positions/{ACCOUNT_ID}", "auth": "{AUTH_TOKEN}"}
{"type": "subscribe", "channel": "user_stats/{ACCOUNT_ID}"}

// Transaktionen via WebSocket
{"type": "jsonapi/sendtx", "data": {"tx_type": INTEGER, "tx_info": ...}}
{"type": "jsonapi/sendtxbatch", "data": {"tx_types": "[INTEGER]", "tx_infos": "[tx_info]"}}
```

### 1.4 Authentifizierung

#### API Keys
- **Index Range**: 0-254 (0,1,2 reserviert für UI)
- **Read-Only Index**: 255 für alle API-Keys
- **Max API Keys**: 252 pro Account

#### Auth Token Format
```
{expiry_unix}:{account_index}:{api_key_index}:{random_hex}
```
- **Max Expiry**: 8 Stunden
- **Read-Only Format**: `ro:{account_index}:{single|all}:{expiry_unix}:{random_hex}`
- **Read-Only Max Expiry**: 10 Jahre

#### Python SDK Beispiel
```python
import lighter

client = lighter.SignerClient(
    url="https://mainnet.zklighter.elliot.ai",
    api_private_keys={API_KEY_INDEX: PRIVATE_KEY},
    account_index=ACCOUNT_INDEX
)

# Auth Token erstellen
auth, err = client.create_auth_token_with_expiry(
    lighter.SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY
)
```

### 1.5 Rate Limits

#### Premium Account
| Metrik | Limit |
|--------|-------|
| REST API | 24.000 gewichtete Requests/Minute |
| WebSocket Connections | 100/IP |
| Subscriptions/Connection | 100 |
| Total Subscriptions | 1000 |
| Max Connections/Minute | 60 |
| Max Messages/Minute | 200 |
| Unique Accounts | 10 |

#### Standard Account
| Metrik | Limit |
|--------|-------|
| REST API | 60 Requests/Minute |
| L2Withdraw | 2/Minute |
| L2UpdateLeverage | 1/Minute |
| L2Transfer | 1/Minute |

### 1.6 Account-Typen

| Feature | Standard | Premium |
|---------|----------|---------|
| **Maker Fee** | 0% | 0.002% (0.2 bps) |
| **Taker Fee** | 0% | 0.02% (2 bps) |
| **Maker Latenz** | 200ms | 0ms |
| **Taker Latenz** | 300ms | 150ms |
| **Cancel Latenz** | 100ms | 0ms |

**Wechsel-Bedingungen**:
- Keine offenen Positionen
- Keine offenen Orders
- 24h seit letztem Wechsel

### 1.7 Order-Typen

```python
# Order Types
ORDER_TYPE_LIMIT = 0
ORDER_TYPE_MARKET = 1
ORDER_TYPE_STOP_LOSS = 2
ORDER_TYPE_STOP_LOSS_LIMIT = 3
ORDER_TYPE_TAKE_PROFIT = 4
ORDER_TYPE_TAKE_PROFIT_LIMIT = 5
ORDER_TYPE_TWAP = 6

# Time in Force
ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
ORDER_TIME_IN_FORCE_POST_ONLY = 2
```

### 1.8 Funding Rate Mechanismus

**Berechnung (stündlich)**:
```
premium_t = (Max(0, ImpactBidPrice - Index) - Max(0, Index - ImpactAskPrice)) / Index

// 1-Stunden TWAP der Minutenwerte
fundingRate = (premium / 8) + interestRateComponent

// Clamped: [-0.5%, +0.5%]

// Zahlung
funding_payment = -1 × position × markPrice × fundingRate
```

**Wichtig**: Das Premium wird durch 8 geteilt, um 8-Stunden-Realisation nachzubilden (wie CEXs).

### 1.9 Mark Price Berechnung

```
Impact Notional = 500 USDC / Initial Margin Fraction
Impact Bid = Avg execution price for market sell of impact notional
Impact Ask = Avg execution price for market buy of impact notional
Impact Price = (Impact Bid + Impact Ask) / 2

price1 = Index + EMA_8min(clamp(ImpactPrice - Index, -Index/200, +Index/200))
price2 = median(CEX mark prices)

Mark Price = Median(ImpactPrice, price1, price2)
```

### 1.10 Liquidations

**Margin Requirements**:
- **IMR**: Initial Margin Requirement
- **MMR**: Maintenance Margin Requirement
- **CMR**: Close-out Margin Requirement (CMR < MMR < IMR)

**Liquidation Waterfall**:
1. **Healthy**: Account Value >= IMR
2. **Pre-Liquidation**: IMR > Account Value >= MMR
3. **Partial Liquidation**: MMR > Account Value > CMR
4. **Full Liquidation**: CMR > Account Value
5. **ADL**: Account Value < 0 und LLP-Kapital reicht nicht

**Zero Price Formel**:
```
zeroPrice_long = markPrice × (1 - (M_i × AccountValue) / MMR)
zeroPrice_short = markPrice × (1 + (M_i × AccountValue) / MMR)
```

### 1.11 PnL Berechnung

```python
# Unrealized PnL
unrealized_pnl = sum((markPrice[i] - avgEntryPrice[i]) × position[i])

# Total Account Value
total_account_value = collateral + unrealized_pnl

# Realized PnL (bei Teilschließung)
delta_collateral = (exitPrice - avgEntryPrice) × delta_position

# Entry Price Update (bei Positionserhöhung)
avgEntryPrice_new = (avgEntryPrice_old × position_old + tradePrice × tradeSize) / position_new
```

### 1.12 Contract Specifications (Auszug)

| Asset | Max Leverage | IMR | MMR | CMR | Price Step | Amount Step |
|-------|--------------|-----|-----|-----|------------|-------------|
| BTC | 50x | 2% | 1.2% | 0.8% | 0.01 | 0.0001 |
| ETH | 50x | 2% | 1.2% | 0.8% | 0.01 | 0.001 |
| SOL | 25x | 4% | 2.4% | 1.6% | 0.001 | 0.001 |
| XRP | 20x | 5% | 3% | 2% | 0.000001 | 1 |
| DOGE | 10x | 10% | 6% | 4% | 0.0001 | 0.01 |

---

## Teil 2: Extended Exchange Ökosystem

### 2.1 Architektur-Übersicht

Extended (ehemals X10) ist eine **Hybrid CLOB Exchange** die derzeit von StarkEx auf Starknet migriert:

```
┌─────────────────────────────────────────────────────────┐
│                Extended Architecture                      │
├─────────────────────────────────────────────────────────┤
│  [Off-Chain]                    [On-Chain]               │
│  ├─ Order Matching              ├─ Settlement (Starknet) │
│  ├─ Position Risk               ├─ Validations           │
│  └─ Transaction Sequencing      └─ Smart Contracts       │
└─────────────────────────────────────────────────────────┘
```

**Settlement**: Zero-Knowledge Proofs werden alle paar Stunden auf Ethereum publiziert.

### 2.2 API-Endpunkte

#### Base URLs
- **Mainnet**: `https://api.x10.exchange` (deprecated)
- **Mainnet**: `https://api.extended.exchange`
- **Testnet**: `https://testnet.extended.exchange`

#### Wichtige REST Endpoints

| Endpoint | Methode | Auth | Beschreibung |
|----------|---------|------|--------------|
| `/api/v1/info/markets` | GET | - | Marktinformationen |
| `/api/v1/info/market-statistics` | GET | - | Marktstatistiken |
| `/api/v1/info/funding-rates` | GET | - | Funding Rate Historie |
| `/api/v1/info/trades` | GET | - | Öffentliche Trades |
| `/api/v1/user/fees` | GET | API Key | Trading Fees |
| `/api/v1/user/balance` | GET | API Key | Account Balance |
| `/api/v1/user/positions` | GET | API Key | Offene Positionen |
| `/api/v1/user/orders` | GET | API Key | Offene Orders |
| `/api/v1/user/leverage` | GET/PUT | API Key | Leverage-Einstellungen |
| `/api/v1/orders` | POST | API Key + Stark | Order erstellen |
| `/api/v1/orders/{id}` | DELETE | API Key + Stark | Order stornieren |
| `/api/v1/orders/cancel-all` | DELETE | API Key + Stark | Alle Orders stornieren |

### 2.3 WebSocket Streams

```javascript
// Orderbook Stream
{"type": "subscribe", "channel": "orderbook", "market": "BTC-USD"}

// Trades Stream
{"type": "subscribe", "channel": "trades", "market": "BTC-USD"}

// Funding Rates Stream
{"type": "subscribe", "channel": "funding-rates", "market": "BTC-USD"}

// Candles Stream (1, 5, 30, 60, 120, 240 min, D)
{"type": "subscribe", "channel": "candles", "market": "BTC-USD", "interval": "60"}

// Account Updates (Auth erforderlich)
{"type": "subscribe", "channel": "account"}
```

### 2.4 Authentifizierung

Extended verwendet **Dual Authentication**:

1. **API Key** (`X-Api-Key` Header)
2. **Stark Signature** (`X-Api-Sign` Header) für Order Management

#### API Sign Generation
```python
import hmac
import hashlib
import base64

def hmac_512(message: str, secret: str) -> str:
    secret_decoded = base64.b64decode(secret)
    result = hmac.new(secret_decoded, message.encode(), hashlib.sha512).digest()
    return base64.b64encode(result).decode()

# Verwendung
signature = hmac_512(f"{method_path}\0{post_data}", api_secret)
```

#### Stark Key Derivation
```python
from x10.perpetual.accounts import StarkPerpetualAccount

# Account aus API Management
stark_account = StarkPerpetualAccount(
    vault=<vault>,
    private_key=private_key,
    public_key=public_key,
    api_key=api_key
)
```

### 2.5 Python SDK

```python
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.orders import OrderSide
from decimal import Decimal

# Account Setup
stark_account = StarkPerpetualAccount(
    vault=vault,
    private_key=private_key,
    public_key=public_key,
    api_key=api_key
)

# Client erstellen
trading_client = PerpetualTradingClient.create(MAINNET_CONFIG, stark_account)

# Order platzieren
order = await trading_client.place_order(
    market_name="BTC-USD",
    amount_of_synthetic=Decimal("1"),
    price=Decimal("63000.1"),
    side=OrderSide.SELL
)

# Order stornieren
await trading_client.orders.cancel_order(order_id=order.id)

# Balance abrufen
balance = await trading_client.account.get_balance()

# Positionen abrufen
positions = await trading_client.account.get_positions()
```

### 2.6 Funding Rate Mechanismus

**Berechnung (minütlich, Anwendung stündlich)**:
```
Premium_Index = (Max(0, ImpactBid - Index) - Max(0, Index - ImpactAsk)) / Index

TWAP_Premium = (1×P1 + 2×P2 + ... + 60×P60) / (1+2+...+60)

Funding_Rate = clamp(TWAP_Premium / 8, -FundingRateCap, +FundingRateCap)
```

**Wichtig**:
- Funding wird jede Minute berechnet
- Anwendung erfolgt stündlich
- Premium wird durch 8 geteilt für 8-Stunden-Realisation

### 2.7 Margin & Liquidation

**Margin Schedule (dynamisch)**:
```
Initial_Margin = min(100%, max(Base_IMR + n × Incremental_IMR, 1/Leverage))

wobei:
n = RoundUp((Position_Value - Base_Risk_Limit) / Risk_Step)
Position_Value = Mark_Price × Position_Size

Maintenance_Margin = Initial_Margin × 0.5
```

**Liquidation**: Margin Ratio > 100%
```
Margin_Ratio = (MMR + Closing_Fees) / Equity
```

### 2.8 Account Balance Formeln

```python
# Equity
equity = wallet_balance + unrealized_pnl

# Available Balance (Trading)
available_trading = wallet_balance + unrealized_pnl - initial_margin_requirement

# Available Balance (Withdrawal)
available_withdrawal = wallet_balance + min(0, unrealized_pnl) - initial_margin_requirement

# Margin Ratio
margin_ratio = (maintenance_margin + closing_fees) / equity
```

### 2.9 Withdrawals

| Typ | Dauer | Limit | L1 TX erforderlich |
|-----|-------|-------|-------------------|
| **Slow** | Bis 20h | Unbegrenzt | Ja (Claim) |
| **Fast** | Sofort | $50,000 | Nein |

**Slow Withdrawal**: 2-Schritt-Prozess
1. L2 Withdrawal Request
2. L1 Claim nach ZK-Proof Generation

### 2.10 Starknet Migration

Extended migriert von StarkEx (privat) auf Starknet (öffentlich):

**Vorteile**:
- Native DeFi-Integrations (Lending, Options)
- Permissionless Innovation
- Niedrigere Kosten bei Skalierung
- Cross-Asset Collateral Unified Margin

**SDK-Konfigurationen**:
- `MAINNET_CONFIG`: Neue Accounts (Starknet)
- `MAINNET_CONFIG_LEGACY_SIGNING_DOMAIN`: Legacy Accounts (app.x10.exchange)
- `TESTNET_CONFIG`: Testnet

---

## Teil 3: SDK-Referenz

### 3.1 Lighter SDKs

| SDK | Sprache | Repository | Status |
|-----|---------|------------|--------|
| lighter-python | Python | github.com/elliottech/lighter-python | Stabil (v1.0.2) |
| lighter-go | Go | github.com/elliottech/lighter-go | Stabil |
| lighter-rs | Rust | crates.io/crates/lighter-rs | Community |

#### Python SDK Installation
```bash
pip install git+https://github.com/elliottech/lighter-python.git
```

#### API-Klassen (Python)
```python
import lighter

# API Client
client = lighter.ApiClient()

# Account API
account_api = lighter.AccountApi(client)
account = await account_api.account(by="index", value="1")

# Order API
order_api = lighter.OrderApi(client)
orderbook = await order_api.order_book_details(market_id=0)

# Transaction API
tx_api = lighter.TransactionApi(client)
nonce = await tx_api.next_nonce(account_index=1, api_key_index=3)

# Signer Client
signer = lighter.SignerClient(
    url=BASE_URL,
    api_private_keys={API_KEY_INDEX: PRIVATE_KEY},
    account_index=ACCOUNT_INDEX
)
```

### 3.2 Extended SDKs

| SDK | Sprache | Repository | Status |
|-----|---------|------------|--------|
| python_sdk | Python | github.com/x10xchange/python_sdk | Stabil (v0.0.17) |
| extended-sdk-golang | Go | github.com/x10xchange/extended-sdk-golang | WIP |
| examples (TypeScript) | TypeScript | github.com/x10xchange/examples | Referenz |

#### Python SDK Installation
```bash
pip install x10-python-trading-starknet
```

#### Trading Client
```python
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG

# Module
trading_client.orders     # Order Management
trading_client.account    # Account Operations
trading_client.markets_info  # Market Information
```

---

## Teil 4: Smart Contract Spezifikationen

### 4.1 Lighter Contracts

**Ethereum Mainnet**:
- **Lighter Proxy**: `0x3B4D...5ca7`
- **Verifier**: ZK Lighter Verifier (Plonky2)
- **Desert Verifier**: Emergency Withdrawal Mode

**Contract-Struktur**:
```
contracts/
├── Factory.sol
├── OrderBook.sol
├── interfaces/
│   ├── IFactory.sol
│   ├── ILighterV2FlashCallback.sol
│   ├── ILighterV2TransferCallback.sol
│   └── IOrderBook.sol
└── libraries/
    ├── Errors.sol
    ├── LinkedList.sol
    └── OrderBookDeployerLib.sol
```

**Data Availability**: Alle Daten für Account Tree Recovery werden als Blobs on-chain publiziert.

### 4.2 Extended/StarkEx Contracts

**StarkEx Smart Contract Architecture**:
1. **Proxy Contract**: System State + Funds Management
2. **Dispatcher Contract**: Business Logic Delegation
3. **Verifier Fact Registry**: ZK Proof Verification
4. **Committee Fact Registry**: Custom Constraints

**Upgrade Flow**:
1. Neue Version registrieren
2. Timelock (User Exit ermöglichen)
3. Version Switch nach Timelock

**Starknet Migration**: Neue Cairo-basierte Contracts auf Starknet

---

## Teil 5: Vergleichsanalyse

### 5.1 Technologie-Vergleich

| Merkmal | Lighter | Extended |
|---------|---------|----------|
| **Proof System** | SNARK (Plonky2) | STARK (StarkEx/Cairo) |
| **L1 Settlement** | Ethereum | Ethereum (via Starknet) |
| **Matching** | ZK-Verified On-L2 | Off-Chain + On-Chain Validation |
| **Customization** | App-Specific Circuits | General Purpose ZK |
| **Programming** | Custom Circuits | Cairo |
| **Trust Model** | Trustless (ZK Proofs) | Trustless (Starknet) |

### 5.2 API-Vergleich

| Feature | Lighter | Extended |
|---------|---------|----------|
| **WebSocket** | Ja | Ja |
| **REST** | Ja | Ja |
| **Batch TX** | 50/Batch | Nein |
| **Auth** | API Key + Signature | API Key + Stark Signature |
| **Rate Limits** | Weight-Based | TBD |

### 5.3 Funding Rate Vergleich

| Aspekt | Lighter | Extended |
|--------|---------|----------|
| **Berechnung** | Minütlich | Minütlich |
| **Anwendung** | Stündlich | Stündlich |
| **Premium Division** | /8 | /8 |
| **Cap** | ±0.5%/h | Konfigurierbar |

### 5.4 Fee-Struktur

| Account Typ | Lighter Standard | Lighter Premium | Extended |
|-------------|------------------|-----------------|----------|
| **Maker** | 0% | 0.002% | Volumen-basiert |
| **Taker** | 0% | 0.02% | Volumen-basiert |

---

## Teil 6: Best Practices für Integration

### 6.1 Lighter Integration

```python
# 1. System Setup
import lighter

# Private Key aus Ethereum Wallet ableiten
client = lighter.SignerClient(
    url="https://mainnet.zklighter.elliot.ai",
    private_key=API_KEY_PRIVATE_KEY,
    account_index=ACCOUNT_INDEX,
    api_key_index=API_KEY_INDEX
)

# 2. Nonce Management
# SDK handled automatisch, oder:
tx_api = lighter.TransactionApi(client)
nonce = await tx_api.next_nonce(account_index=ACCOUNT_INDEX, api_key_index=API_KEY_INDEX)

# 3. Order Signierung
tx_type, tx_info, tx_hash = client.sign_create_order(
    market_index=0,
    base_amount=100000,  # Integer
    price=330000,        # Integer
    is_ask=True,
    order_type=0,        # LIMIT
    time_in_force=1,     # GTT
    client_order_index=unique_id
)

# 4. Transaktion senden
response = await tx_api.send_tx(tx_type=tx_type, tx_info=tx_info)
```

### 6.2 Extended Integration

```python
# 1. Onboarding via SDK
from x10.perpetual.user_client.user_client import UserClient

user_client = UserClient(eth_private_key, MAINNET_CONFIG)
onboarded = await user_client.onboard(referral_code=None)

# 2. Trading Setup
from x10.perpetual.trading_client import PerpetualTradingClient

trading_client = PerpetualTradingClient.create(
    MAINNET_CONFIG,
    onboarded.stark_account
)

# 3. Deposit
await trading_client.account.deposit(amount=Decimal("1000"))

# 4. Trading
order = await trading_client.place_order(
    market_name="BTC-USD",
    amount_of_synthetic=Decimal("0.1"),
    price=Decimal("45000"),
    side=OrderSide.BUY
)
```

### 6.3 Wichtige Hinweise

#### Lighter-spezifisch
- `account_index = 0` explizit vs. `None` (Adapter-Default) unterscheiden
- Auth Tokens maximal 8h gültig
- Premium Account für HFT empfohlen

#### Extended-spezifisch
- Legacy Accounts: `MAINNET_CONFIG_LEGACY_SIGNING_DOMAIN` verwenden
- Stark Key wird aus Ethereum Account abgeleitet
- Subaccounts haben separate Stark Keys

---

## Teil 4: Code-Level Details (Deep Dive)

### 4.1 Lighter Transaction Bit-Serialisierung

Die Lighter SDK nutzt native Binary-Signer (.so/.dll/.dylib) via `ctypes` für hochperformante Transaktions-Signierung.

#### CTypes Strukturen für Order-Erstellung

```python
import ctypes
from pathlib import Path

# Transaction Type Constants
TX_TYPE_CREATE_ORDER = 14
TX_TYPE_CANCEL_ORDER = 15
TX_TYPE_CANCEL_ALL_ORDERS = 16
TX_TYPE_MODIFY_ORDER = 17
TX_TYPE_UPDATE_LEVERAGE = 20
TX_TYPE_CHANGE_PUB_KEY = 8
TX_TYPE_CREATE_SUB_ACCOUNT = 9

# Order Type Constants
ORDER_TYPE_LIMIT = 0
ORDER_TYPE_LIMIT_MAKER = 1
ORDER_TYPE_IMMEDIATE_OR_CANCEL = 2
ORDER_TYPE_FILL_OR_KILL = 3
ORDER_TYPE_STOP_LOSS = 4
ORDER_TYPE_STOP_LOSS_LIMIT = 5
ORDER_TYPE_TAKE_PROFIT = 6
ORDER_TYPE_TAKE_PROFIT_LIMIT = 7

# Time-in-Force Constants
TIF_GTC = 0  # Good Till Cancel
TIF_ALO = 1  # Add Liquidity Only
TIF_IOC = 2  # Immediate or Cancel
TIF_FOK = 3  # Fill or Kill

class CreateOrderTxReq(ctypes.Structure):
    """Binary structure for order creation requests."""
    _fields_ = [
        ("MarketIndex", ctypes.c_uint8),      # 1 byte: Market ID
        ("ClientOrderIndex", ctypes.c_longlong),  # 8 bytes: Client order ID
        ("BaseAmount", ctypes.c_longlong),    # 8 bytes: Order size (scaled)
        ("Price", ctypes.c_uint32),           # 4 bytes: Price (scaled)
        ("IsAsk", ctypes.c_uint8),            # 1 byte: 1=Sell, 0=Buy
        ("Type", ctypes.c_uint8),             # 1 byte: Order type enum
        ("TimeInForce", ctypes.c_uint8),      # 1 byte: TIF enum
        ("ReduceOnly", ctypes.c_uint8),       # 1 byte: Reduce-only flag
        ("TriggerPrice", ctypes.c_uint32),    # 4 bytes: For stop/TP orders
        ("OrderExpiry", ctypes.c_longlong),   # 8 bytes: Unix timestamp
    ]

class SignedTxResponse(ctypes.Structure):
    """Response from native signer."""
    _fields_ = [
        ("txType", ctypes.c_uint8),
        ("txInfo", ctypes.c_char_p),
        ("txHash", ctypes.c_char_p),
        ("messageToSign", ctypes.c_char_p),
        ("err", ctypes.c_char_p),
    ]

class CancelOrderTxReq(ctypes.Structure):
    """Binary structure for order cancellation."""
    _fields_ = [
        ("MarketIndex", ctypes.c_uint8),
        ("ClientOrderIndex", ctypes.c_longlong),
    ]

class ModifyOrderTxReq(ctypes.Structure):
    """Binary structure for order modification."""
    _fields_ = [
        ("MarketIndex", ctypes.c_uint8),
        ("ClientOrderIndex", ctypes.c_longlong),
        ("NewClientOrderIndex", ctypes.c_longlong),
        ("NewBaseAmount", ctypes.c_longlong),
        ("NewPrice", ctypes.c_uint32),
    ]
```

#### Native Signer Loading

```python
import platform
from pathlib import Path

def load_native_signer() -> ctypes.CDLL:
    """Load the appropriate native signer for the platform."""
    system = platform.system()

    if system == "Darwin":
        lib_name = "signer_darwin.dylib"
    elif system == "Linux":
        lib_name = "signer_linux.so"
    elif system == "Windows":
        lib_name = "signer_windows.dll"
    else:
        raise RuntimeError(f"Unsupported platform: {system}")

    # Library is bundled in lighter/signers/ directory
    lib_path = Path(__file__).parent / "signers" / lib_name

    signer = ctypes.CDLL(str(lib_path))

    # Configure function signatures
    signer.CreateAndSignOrderTx.argtypes = [
        ctypes.c_char_p,     # privateKey
        ctypes.c_uint8,      # accountIndex
        ctypes.c_uint64,     # nonce
        ctypes.POINTER(CreateOrderTxReq),
    ]
    signer.CreateAndSignOrderTx.restype = SignedTxResponse

    return signer
```

#### Vollständiges Signing-Beispiel

```python
def create_signed_order(
    private_key: str,
    account_index: int,
    nonce: int,
    market_index: int,
    size: float,
    price: float,
    is_sell: bool,
    order_type: int = ORDER_TYPE_LIMIT,
    time_in_force: int = TIF_GTC,
    reduce_only: bool = False,
    client_order_id: int = None,
) -> dict:
    """Create and sign an order using the native signer."""

    signer = load_native_signer()

    # Scale values according to market precision
    # Lighter uses fixed-point with 10^8 scale for amounts
    AMOUNT_SCALE = 10**8
    PRICE_SCALE = 10**6

    scaled_size = int(size * AMOUNT_SCALE)
    scaled_price = int(price * PRICE_SCALE)

    req = CreateOrderTxReq()
    req.MarketIndex = market_index
    req.ClientOrderIndex = client_order_id or int(time.time() * 1000)
    req.BaseAmount = scaled_size
    req.Price = scaled_price
    req.IsAsk = 1 if is_sell else 0
    req.Type = order_type
    req.TimeInForce = time_in_force
    req.ReduceOnly = 1 if reduce_only else 0
    req.TriggerPrice = 0
    req.OrderExpiry = 0  # No expiry

    response = signer.CreateAndSignOrderTx(
        private_key.encode(),
        account_index,
        nonce,
        ctypes.byref(req),
    )

    if response.err:
        raise ValueError(f"Signing failed: {response.err.decode()}")

    return {
        "tx_type": response.txType,
        "tx_info": response.txInfo.decode(),
        "tx_hash": response.txHash.decode(),
    }
```

### 4.2 Extended/X10 Starknet Signing

Extended Exchange migrierte im August 2025 von StarkEx auf Starknet. Das Signing nutzt EIP-712-style typed data mit Stark-Kurven-Kryptographie.

#### Starknet Domain Konfiguration

```python
from dataclasses import dataclass
from typing import Tuple

@dataclass
class StarknetDomain:
    """EIP-712 style domain for Starknet signing."""
    name: str          # "Perpetuals"
    version: str       # "v0"
    chain_id: str      # "SN_MAIN" or "SN_SEPOLIA"
    revision: str      # "1"

# Production Configuration
MAINNET_CONFIG = {
    "api_base_url": "https://api.starknet.extended.exchange/api/v1",
    "stream_url": "wss://api.starknet.extended.exchange/stream.extended.exchange/v1",
    "signing_domain": "extended.exchange",
    "starknet_domain": StarknetDomain(
        name="Perpetuals",
        version="v0",
        chain_id="SN_MAIN",
        revision="1",
    ),
}

# Testnet Configuration
TESTNET_CONFIG = {
    "api_base_url": "https://api.starknet.sepolia.extended.exchange/api/v1",
    "stream_url": "wss://api.starknet.sepolia.extended.exchange/stream.extended.exchange/v1",
    "signing_domain": "extended.exchange",
    "starknet_domain": StarknetDomain(
        name="Perpetuals",
        version="v0",
        chain_id="SN_SEPOLIA",
        revision="1",
    ),
}

# Legacy StarkEx (deprecated)
LEGACY_STARKEX_CONFIG = {
    "api_base_url": "https://api.x10.exchange/api/v1",
    "stream_url": "wss://stream.x10.exchange/v1",
}
```

#### Stark Signature Implementation

```python
from fast_stark_crypto import sign, pedersen_hash, verify

class StarkPerpetualAccount:
    """Account class for Extended Exchange with Stark signing."""

    def __init__(self, private_key: int, public_key: int, account_id: int):
        self.__private_key = private_key
        self.public_key = public_key
        self.account_id = account_id

    def sign(self, msg_hash: int) -> Tuple[int, int]:
        """
        Sign a message hash using the Stark curve.

        Returns (r, s) signature tuple.
        """
        return sign(private_key=self.__private_key, msg_hash=msg_hash)

    def sign_order(self, order_data: dict) -> Tuple[int, int]:
        """Sign an order for submission."""
        # Compute Pedersen hash of order fields
        msg_hash = self._compute_order_hash(order_data)
        return self.sign(msg_hash)

    def _compute_order_hash(self, order_data: dict) -> int:
        """Compute Pedersen hash for order data."""
        # Order hash computation follows EIP-712 structure
        fields = [
            int(order_data["market_id"]),
            int(order_data["side"]),  # 0=BUY, 1=SELL
            int(order_data["type"]),  # LIMIT=0, MARKET=1
            int(float(order_data["quantity"]) * 10**18),
            int(float(order_data["price"]) * 10**18),
            int(order_data["nonce"]),
            int(order_data["expiration"]),
        ]

        # Sequential Pedersen hashing
        result = fields[0]
        for field in fields[1:]:
            result = pedersen_hash(result, field)

        return result

# Account derivation from L1 signature
def derive_stark_key_from_eth_signature(
    eth_signature: str,
    eth_address: str,
) -> Tuple[int, int]:
    """
    Derive Stark keypair from Ethereum signature.

    This follows the StarkEx/Starknet key derivation standard.
    """
    from hashlib import sha256

    # Grind signature to valid Stark key
    seed = sha256(bytes.fromhex(eth_signature[2:])).digest()

    # Apply key grinding algorithm
    private_key = int.from_bytes(seed, "big") % STARK_PRIME

    # Derive public key using Stark curve multiplication
    public_key = stark_curve_multiply(GENERATOR, private_key)

    return private_key, public_key

STARK_PRIME = 2**251 + 17 * 2**192 + 1
```

#### WebSocket Authentifizierung (Extended)

```python
import asyncio
import json
import time
import websockets
from typing import Optional

async def connect_extended_ws(
    api_key: str,
    account: StarkPerpetualAccount,
    config: dict = MAINNET_CONFIG,
) -> websockets.WebSocketClientProtocol:
    """
    Establish authenticated WebSocket connection to Extended Exchange.
    """

    # Generate authentication signature
    timestamp = int(time.time() * 1000)
    auth_message = f"{timestamp}"
    msg_hash = pedersen_hash(
        int.from_bytes(auth_message.encode(), "big"),
        account.account_id,
    )
    r, s = account.sign(msg_hash)

    # Connect to WebSocket
    ws = await websockets.connect(
        config["stream_url"],
        ping_interval=25,
        ping_timeout=10,
    )

    # Send authentication message
    auth_payload = {
        "type": "auth",
        "apiKey": api_key,
        "timestamp": timestamp,
        "signature": {
            "r": hex(r),
            "s": hex(s),
        },
        "accountId": account.account_id,
    }
    await ws.send(json.dumps(auth_payload))

    # Wait for auth response
    response = json.loads(await ws.recv())
    if response.get("status") != "OK":
        raise ValueError(f"Authentication failed: {response}")

    return ws

async def subscribe_to_orders(
    ws: websockets.WebSocketClientProtocol,
    account_id: int,
):
    """Subscribe to order updates for an account."""
    subscribe_msg = {
        "type": "subscribe",
        "channel": "orders",
        "accountId": account_id,
    }
    await ws.send(json.dumps(subscribe_msg))
```

### 4.3 Error Codes Reference

#### Lighter Error Codes

```python
# Transaction Errors
LIGHTER_ERROR_CODES = {
    # General Transaction Errors
    21500: "Transaction not found",
    21501: "Invalid transaction info",
    21502: "Invalid signature",
    21503: "Nonce already used",
    21504: "Nonce too old",
    21505: "Account not found",
    21506: "Too many pending transactions",
    21507: "Account below maintenance margin (liquidation risk)",
    21508: "Account below initial margin (cannot open position)",
    21509: "Invalid account index",
    21510: "API key not authorized for this operation",

    # Order-Specific Errors
    21600: "Order is not an active limit order",
    21601: "Order not found",
    21602: "Invalid market index",
    21603: "Market is paused",
    21604: "Order size below minimum",
    21605: "Order size exceeds maximum",
    21606: "Price out of bounds",
    21607: "Self-trade prevention triggered",
    21608: "Post-only order would take liquidity",
    21609: "Reduce-only order would increase position",
    21610: "Maximum open orders exceeded",

    # Margin/Leverage Errors
    21700: "Invalid leverage value",
    21701: "Leverage exceeds market maximum",
    21702: "Insufficient collateral for leverage change",
    21703: "Position would exceed risk limits",
}

def handle_lighter_error(error_code: int, message: str = "") -> str:
    """Get human-readable error description."""
    base_msg = LIGHTER_ERROR_CODES.get(error_code, f"Unknown error: {error_code}")
    return f"{base_msg}. {message}" if message else base_msg
```

#### Extended Error Codes / Status Reasons

```python
from enum import StrEnum

class OrderStatusReason(StrEnum):
    """Reasons for order rejection or cancellation on Extended."""

    # Success
    NONE = "NONE"  # Order accepted successfully

    # Market Errors
    UNKNOWN_MARKET = "UNKNOWN_MARKET"      # Market does not exist
    DISABLED_MARKET = "DISABLED_MARKET"    # Market is not active

    # Balance/Margin Errors
    NOT_ENOUGH_FUNDS = "NOT_ENOUGH_FUNDS"  # Insufficient balance
    INVALID_LEVERAGE = "INVALID_LEVERAGE"  # Leverage out of bounds

    # Liquidity Errors
    NO_LIQUIDITY = "NO_LIQUIDITY"          # No counterparty liquidity

    # Order Validation Errors
    INVALID_FEE = "INVALID_FEE"            # Fee parameter invalid
    INVALID_QTY = "INVALID_QTY"            # Quantity invalid
    INVALID_PRICE = "INVALID_PRICE"        # Price invalid
    INVALID_VALUE = "INVALID_VALUE"        # Order exceeds max value

    # Account Errors
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"    # Account does not exist

    # Execution Errors
    SELF_TRADE_PROTECTION = "SELF_TRADE_PROTECTION"  # Would match own order
    POST_ONLY_FAILED = "POST_ONLY_FAILED"            # Would take liquidity
    REDUCE_ONLY_FAILED = "REDUCE_ONLY_FAILED"        # Would increase position
    IOC_CANCELLED = "IOC_CANCELLED"                  # IOC not fully filled
    FOK_CANCELLED = "FOK_CANCELLED"                  # FOK not fillable

    # Reference Errors
    PREV_ORDER_NOT_FOUND = "PREV_ORDER_NOT_FOUND"    # Amend target not found
    DUPLICATE_ORDER_ID = "DUPLICATE_ORDER_ID"        # External ID collision

    # System Errors
    UNKNOWN = "UNKNOWN"                    # Technical/internal error
    SYSTEM_OVERLOAD = "SYSTEM_OVERLOAD"    # Rate limiting active
    MARKET_HALTED = "MARKET_HALTED"        # Trading halted

# Error handling helper
def is_retriable_error(reason: OrderStatusReason) -> bool:
    """Check if an error is temporary and worth retrying."""
    retriable = {
        OrderStatusReason.NO_LIQUIDITY,
        OrderStatusReason.SYSTEM_OVERLOAD,
        OrderStatusReason.UNKNOWN,
    }
    return reason in retriable
```

### 4.4 Rate Limiting Details

#### Lighter Rate Limits

```python
# Rate limit configuration by account type
LIGHTER_RATE_LIMITS = {
    "premium": {
        "requests_per_minute": 24000,
        "weight_based": True,
        "maker_latency_ms": 0,  # No artificial delay
        "taker_latency_ms": 0,
    },
    "standard": {
        "requests_per_minute": 60,
        "weight_based": True,
        "maker_latency_ms": 1000,  # 1 second delay for maker
        "taker_latency_ms": 0,
    },
}

# Response headers (standard pattern, not Lighter-specific)
# X-RateLimit-Limit: Maximum requests in window
# X-RateLimit-Remaining: Requests remaining
# X-RateLimit-Reset: Unix timestamp when window resets
```

#### Extended Rate Limits

```python
EXTENDED_RATE_LIMITS = {
    "public_api": {
        "requests_per_second": 10,
        "requests_per_minute": 300,
    },
    "private_api": {
        "requests_per_second": 20,
        "requests_per_minute": 600,
    },
    "websocket": {
        "messages_per_second": 50,
        "subscriptions_max": 100,
    },
}
```

### 4.5 Margin & Liquidation Formulas

#### Lighter Margin Requirements

```python
from dataclasses import dataclass
from typing import List

@dataclass
class MarginConfig:
    """Market-specific margin configuration."""
    initial_margin_fraction: float    # IMR
    maintenance_margin_fraction: float  # MMR
    closeout_margin_fraction: float   # CMR
    max_leverage: int

# Example: BTC-USD market
BTC_USD_MARGIN = MarginConfig(
    initial_margin_fraction=0.05,      # 5% = 20x max leverage
    maintenance_margin_fraction=0.03,  # 3%
    closeout_margin_fraction=0.01,     # 1%
    max_leverage=20,
)

def calculate_margin_requirements(
    positions: List[dict],
    margin_config: dict,  # market_index -> MarginConfig
) -> dict:
    """
    Calculate margin requirements for an account.

    Returns:
        dict with initial_margin_req, maintenance_margin_req, closeout_margin_req
    """
    imr_total = 0.0
    mmr_total = 0.0
    cmr_total = 0.0

    for pos in positions:
        size = abs(pos["size"])
        mark_price = pos["mark_price"]
        config = margin_config[pos["market_index"]]

        position_value = size * mark_price

        imr_total += position_value * config.initial_margin_fraction
        mmr_total += position_value * config.maintenance_margin_fraction
        cmr_total += position_value * config.closeout_margin_fraction

    return {
        "initial_margin_req": imr_total,
        "maintenance_margin_req": mmr_total,
        "closeout_margin_req": cmr_total,
    }

def calculate_liquidation_price(
    position_size: float,
    entry_price: float,
    collateral: float,
    mmr: float,
    is_long: bool,
) -> float:
    """
    Calculate the liquidation price for a position.

    Lighter Formel:
    - Long: liq_price = entry_price - (collateral - mmr * position_value) / position_size
    - Short: liq_price = entry_price + (collateral - mmr * position_value) / position_size
    """
    position_value = abs(position_size) * entry_price
    margin_buffer = collateral - (mmr * position_value)

    if is_long:
        liq_price = entry_price - (margin_buffer / abs(position_size))
    else:
        liq_price = entry_price + (margin_buffer / abs(position_size))

    return max(0, liq_price)

def calculate_zero_price(
    mark_price: float,
    account_value: float,
    maintenance_margin_req: float,
    market_mmr: float,
    is_short: bool,
) -> float:
    """
    Lighter's 'Zero Price' for partial liquidation.

    Trades at zero price maintain TAV/MMR ratio constant.
    """
    if maintenance_margin_req == 0:
        return mark_price

    ratio = (market_mmr * account_value) / maintenance_margin_req

    if is_short:
        return mark_price * (1 + ratio)
    else:
        return mark_price * (1 - ratio)
```

### 4.6 Auto-Deleveraging (ADL) System

#### Extended ADL Implementation

```python
@dataclass
class ADLStatus:
    """ADL ranking information for a position."""
    percentile: float  # 0-100, higher = more likely to be ADL'd
    level: int         # 1-4, where 4 is highest risk

def interpret_adl_level(level: int) -> str:
    """Interpret ADL risk level."""
    levels = {
        1: "Low risk - Bottom quartile of ADL queue",
        2: "Moderate risk - Second quartile",
        3: "Elevated risk - Third quartile",
        4: "High risk - Top quartile, likely to be ADL'd first",
    }
    return levels.get(level, "Unknown")

# From Extended API response:
# "adl": "2.5" -> Position is in bottom 2.5% of ADL queue (very safe)
# "adl": "97.5" -> Position is in top 2.5% (high risk)

def calculate_adl_ranking(
    unrealized_pnl: float,
    effective_leverage: float,
    position_side: str,
) -> float:
    """
    Calculate ADL queue ranking.

    Higher profit + higher leverage = higher priority for ADL.
    Opposing side to liquidated position gets deleveraged.
    """
    # Simplified ranking formula (actual varies by exchange)
    if unrealized_pnl <= 0:
        return 0.0  # Losing positions don't get ADL'd

    # Profit-weighted leverage score
    score = unrealized_pnl * effective_leverage
    return score

def should_reduce_adl_risk(adl_percentile: float, threshold: float = 80.0) -> bool:
    """Check if position should be reduced to lower ADL risk."""
    return adl_percentile >= threshold
```

#### ADL Mitigation Strategies

```python
def mitigate_adl_risk(
    position: dict,
    current_adl_percentile: float,
    target_percentile: float = 50.0,
) -> dict:
    """
    Calculate position adjustment to reduce ADL risk.

    Options:
    1. Reduce position size (reduces absolute profit exposure)
    2. Reduce leverage (reduces effective leverage component)
    3. Close position partially at current profit
    """
    if current_adl_percentile <= target_percentile:
        return {"action": "none", "reason": "ADL risk acceptable"}

    # Calculate reduction needed
    reduction_ratio = (current_adl_percentile - target_percentile) / current_adl_percentile

    suggested_size = position["size"] * (1 - reduction_ratio)

    return {
        "action": "reduce_position",
        "current_size": position["size"],
        "suggested_size": suggested_size,
        "reduction_pct": reduction_ratio * 100,
        "reason": f"ADL percentile {current_adl_percentile}% exceeds target {target_percentile}%",
    }
```

### 4.7 Order Types Reference (2025/2026)

#### Lighter Order Types

```python
class LighterOrderType:
    """All order types supported by Lighter (as of 2026)."""

    # Basic Types
    LIMIT = 0                  # Standard limit order
    LIMIT_MAKER = 1            # Post-only limit (rejected if would take)
    IMMEDIATE_OR_CANCEL = 2    # Fill what's possible, cancel rest
    FILL_OR_KILL = 3           # Fill entirely or cancel entirely

    # Conditional Types
    STOP_LOSS = 4              # Market order triggered at stop price
    STOP_LOSS_LIMIT = 5        # Limit order triggered at stop price
    TAKE_PROFIT = 6            # Market order triggered at TP price
    TAKE_PROFIT_LIMIT = 7      # Limit order triggered at TP price

    # Note: OCO (One-Cancels-Other) is NOT natively supported
    # Must be implemented client-side with linked stop/TP orders

class LighterTimeInForce:
    GTC = 0   # Good Till Cancel
    ALO = 1   # Add Liquidity Only (same as LIMIT_MAKER)
    IOC = 2   # Immediate or Cancel
    FOK = 3   # Fill or Kill
```

#### Extended Order Types

```python
class ExtendedOrderType:
    """All order types supported by Extended Exchange (as of 2026)."""

    LIMIT = "LIMIT"            # Standard limit order
    MARKET = "MARKET"          # Market order
    CONDITIONAL = "CONDITIONAL"  # Stop/TP orders
    TPSL = "TPSL"              # Combined Take-Profit / Stop-Loss
    TWAP = "TWAP"              # Time-Weighted Average Price

    # Note: Extended DOES support TPSL as single order type
    # which automatically handles OCO-like behavior

class ExtendedTimeInForce:
    GTC = "GTC"    # Good Till Cancel
    IOC = "IOC"    # Immediate or Cancel
    FOK = "FOK"    # Fill or Kill
    GTD = "GTD"    # Good Till Date
    POST_ONLY = "POST_ONLY"  # Maker only
```

### 4.8 WebSocket Order Placement

#### Lighter WebSocket Trading

```python
import json
import asyncio
import websockets

async def place_order_via_ws(
    ws: websockets.WebSocketClientProtocol,
    signed_tx: dict,
) -> dict:
    """
    Place an order via WebSocket (lower latency than REST).

    Note: Lighter supports full trading via WebSocket.
    """
    message = {
        "type": "jsonapi/sendtx",
        "data": {
            "tx_type": signed_tx["tx_type"],
            "tx_info": signed_tx["tx_info"],
        },
    }

    await ws.send(json.dumps(message))

    # Response comes on the same connection
    response = json.loads(await ws.recv())

    if response.get("error"):
        raise ValueError(f"Order failed: {response['error']}")

    return response

async def place_batch_orders_via_ws(
    ws: websockets.WebSocketClientProtocol,
    signed_txs: list,
) -> dict:
    """Place multiple orders in a single batch (max 50)."""

    if len(signed_txs) > 50:
        raise ValueError("Maximum 50 orders per batch")

    message = {
        "type": "jsonapi/sendtxbatch",
        "data": {
            "tx_types": json.dumps([tx["tx_type"] for tx in signed_txs]),
            "tx_infos": json.dumps([tx["tx_info"] for tx in signed_txs]),
        },
    }

    await ws.send(json.dumps(message))
    return json.loads(await ws.recv())
```

#### Extended WebSocket Trading

```python
async def place_order_extended_ws(
    ws: websockets.WebSocketClientProtocol,
    account: StarkPerpetualAccount,
    order: dict,
) -> dict:
    """
    Place an order via Extended WebSocket.

    Extended currently supports order placement via REST only.
    WebSocket is for market data and order status updates.

    For lowest latency, use REST with keep-alive connections.
    """
    # Sign the order
    signature = account.sign_order(order)

    # Extended uses REST for order submission
    # WebSocket provides real-time updates
    raise NotImplementedError(
        "Extended does not support WebSocket order placement. "
        "Use REST API with Connection: keep-alive for lowest latency."
    )
```

### 4.9 Starknet Migration Notes (2025)

Extended Exchange completed migration from StarkEx to Starknet in August 2025.

**Key Changes**:

| Aspect | StarkEx (Legacy) | Starknet (Current) |
|--------|------------------|-------------------|
| **API URL** | api.x10.exchange | api.starknet.extended.exchange |
| **Signing** | StarkEx typed data | Starknet typed data v0 |
| **Chain ID** | N/A | SN_MAIN / SN_SEPOLIA |
| **Domain Name** | x10.exchange | Perpetuals |
| **Deposits** | StarkEx vault | Starknet bridge |
| **Withdrawals** | StarkEx | Starknet (+ fast bridge) |

**Migration Code Update**:

```python
# OLD (StarkEx - deprecated)
config = {
    "api_url": "https://api.x10.exchange/api/v1",
    "signing_domain": "x10.exchange",
}

# NEW (Starknet - current)
config = {
    "api_url": "https://api.starknet.extended.exchange/api/v1",
    "signing_domain": "extended.exchange",
    "starknet_domain": {
        "name": "Perpetuals",
        "version": "v0",
        "chain_id": "SN_MAIN",
        "revision": "1",
    },
}
```

---

## Teil 5: Historische Daten & Analytik für Z-Score Berechnung

Diese Sektion beschreibt detailliert, wie historische Funding-Rate-Daten für Z-Score-Berechnungen und Backtesting abgefragt werden.

### 5.1 Lighter: Funding-Rate-Historie abrufen

#### Endpoint: `/api/v1/fundings`

```python
import requests
from datetime import datetime, timedelta

BASE_URL = "https://mainnet.zklighter.elliot.ai"

def get_lighter_funding_history(
    market_id: int,
    days_back: int = 30,
    resolution: str = "1h",
) -> list:
    """
    Abrufen der Funding-Rate-Historie für Lighter.

    Args:
        market_id: Market-Index (0=ETH, 1=BTC, etc.)
        days_back: Anzahl Tage zurück
        resolution: "1h" oder "1d"

    Returns:
        Liste von Funding-Rate-Einträgen
    """
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=days_back)).timestamp() * 1000)

    params = {
        "market_id": market_id,
        "resolution": resolution,
        "start_timestamp": start_time,
        "end_timestamp": end_time,
        "count_back": days_back * (24 if resolution == "1h" else 1),
    }

    response = requests.get(f"{BASE_URL}/api/v1/fundings", params=params)
    data = response.json()

    return data.get("fundings", [])

# Beispiel: 30 Tage ETH Funding-Historie
eth_funding = get_lighter_funding_history(market_id=0, days_back=30)
```

#### Response-Struktur

| Feld | Typ | Beschreibung |
|------|-----|--------------|
| `timestamp` | int64 | Unix-Timestamp in ms |
| `value` | string | Absoluter Funding-Betrag |
| `rate` | string | Funding-Rate (z.B. "0.0001" = 0.01%) |
| `direction` | string | "long" oder "short" |

#### Endpoint: `/api/v1/funding-rates` (Aktuell)

```python
def get_lighter_current_funding_rates() -> dict:
    """Aktuelle Funding-Rates aller Märkte abrufen."""
    response = requests.get(f"{BASE_URL}/api/v1/funding-rates")
    return response.json()

# Response enthält alle Märkte mit aktuellen Rates
# {
#   "funding_rates": [
#     {"market_id": 0, "symbol": "ETH", "rate": 0.0001},
#     {"market_id": 1, "symbol": "BTC", "rate": 0.00015},
#   ]
# }
```

### 5.2 Extended: Funding-Rate-Historie abrufen

#### Endpoint: `/api/v1/info/{market}/funding`

```python
EXTENDED_BASE_URL = "https://api.starknet.extended.exchange/api/v1"

def get_extended_funding_history(
    market: str,
    days_back: int = 30,
    api_key: str = None,
) -> list:
    """
    Abrufen der Funding-Rate-Historie für Extended.

    Args:
        market: Market-Name (z.B. "BTC-USD", "ETH-USD")
        days_back: Anzahl Tage zurück
        api_key: API-Key für höhere Rate-Limits

    Returns:
        Liste von Funding-Rate-Einträgen (max 10.000 pro Anfrage)
    """
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=days_back)).timestamp() * 1000)

    headers = {"X-Api-Key": api_key} if api_key else {}

    all_fundings = []
    cursor = None

    while True:
        params = {
            "startTime": start_time,
            "endTime": end_time,
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor

        response = requests.get(
            f"{EXTENDED_BASE_URL}/info/{market}/funding",
            params=params,
            headers=headers,
        )
        data = response.json()

        if data["status"] != "OK":
            break

        fundings = data.get("data", [])
        all_fundings.extend(fundings)

        # Pagination
        pagination = data.get("pagination", {})
        if pagination.get("count", 0) < 1000:
            break
        cursor = pagination.get("cursor")

    return all_fundings

# Beispiel: 30 Tage BTC Funding-Historie
btc_funding = get_extended_funding_history("BTC-USD", days_back=30)
```

#### Response-Struktur

| Feld | Typ | Beschreibung |
|------|-----|--------------|
| `m` | string | Market-Name (z.B. "BTC-USD") |
| `T` | int64 | Timestamp in ms |
| `f` | string | Funding-Rate (1-Stunden-Rate) |

### 5.3 Z-Score Berechnung aus Funding-Daten

```python
import numpy as np
from typing import List, Dict

def calculate_funding_zscore(
    funding_rates: List[Dict],
    lookback_hours: int = 720,  # 30 Tage
    current_rate: float = None,
) -> dict:
    """
    Berechnet den Z-Score der aktuellen Funding-Rate.

    Z-Score = (current_rate - mean) / std_dev

    Ein hoher positiver Z-Score deutet auf überhöhte Long-Prämien hin,
    ein negativer Z-Score auf überhöhte Short-Prämien.
    """
    # Rates extrahieren
    rates = [float(f.get("rate") or f.get("f", 0)) for f in funding_rates]

    if len(rates) < 24:  # Minimum 1 Tag
        return {"error": "Insufficient data", "zscore": None}

    # Auf lookback beschränken
    rates = rates[-lookback_hours:]

    mean_rate = np.mean(rates)
    std_rate = np.std(rates)

    if std_rate == 0:
        return {"error": "Zero variance", "zscore": 0.0}

    # Aktuelle Rate (letzte oder übergebene)
    current = current_rate if current_rate is not None else rates[-1]

    zscore = (current - mean_rate) / std_rate

    return {
        "zscore": zscore,
        "current_rate": current,
        "mean_rate": mean_rate,
        "std_rate": std_rate,
        "lookback_hours": len(rates),
        "percentile": _zscore_to_percentile(zscore),
    }

def _zscore_to_percentile(zscore: float) -> float:
    """Konvertiert Z-Score zu Perzentile."""
    from scipy import stats
    return stats.norm.cdf(zscore) * 100

# Beispiel: Z-Score für Arbitrage-Signal
lighter_eth = get_lighter_funding_history(market_id=0, days_back=30)
extended_eth = get_extended_funding_history("ETH-USD", days_back=30)

lighter_zscore = calculate_funding_zscore(lighter_eth)
extended_zscore = calculate_funding_zscore(extended_eth)

print(f"Lighter ETH Z-Score: {lighter_zscore['zscore']:.2f}")
print(f"Extended ETH Z-Score: {extended_zscore['zscore']:.2f}")

# Spread Z-Score für Cross-Exchange Arbitrage
spread_signal = lighter_zscore['zscore'] - extended_zscore['zscore']
print(f"Spread Signal: {spread_signal:.2f}")
```

### 5.4 Candles für Mark/Index Price Historie

#### Lighter Candles

```python
def get_lighter_candles(
    market_id: int,
    resolution: str = "1h",  # 1m, 5m, 15m, 30m, 1h, 4h, 12h, 1d, 1w
    days_back: int = 7,
) -> list:
    """
    OHLCV-Daten von Lighter abrufen.
    Max 500 Candles pro Anfrage.
    """
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=days_back)).timestamp() * 1000)

    params = {
        "market_id": market_id,
        "resolution": resolution,
        "start_timestamp": start_time,
        "end_timestamp": end_time,
        "count_back": 500,
    }

    response = requests.get(f"{BASE_URL}/api/v1/candles", params=params)
    data = response.json()

    # Response: {"r": "1h", "c": [{"t": timestamp, "o": open, "h": high, "l": low, "c": close, "v": volume}]}
    return data.get("c", [])
```

#### Extended Candles (inkl. Mark/Index Prices)

```python
def get_extended_candles(
    market: str,
    candle_type: str = "trades",  # trades, mark-prices, index-prices
    interval: str = "1h",
    limit: int = 1000,
) -> list:
    """
    OHLCV oder Mark/Index Price Candles von Extended.
    Max 10.000 Records pro Anfrage.

    candle_type:
    - "trades": Normale OHLCV Candles
    - "mark-prices": Mark Price Historie
    - "index-prices": Index Price Historie
    """
    params = {
        "interval": interval,
        "limit": limit,
    }

    response = requests.get(
        f"{EXTENDED_BASE_URL}/info/candles/{market}/{candle_type}",
        params=params,
    )
    data = response.json()

    # Response: {"data": [{"o": open, "c": close, "h": high, "l": low, "v": volume, "T": timestamp}]}
    return data.get("data", [])

# Mark Price Historie für Premium-Analyse
btc_mark_prices = get_extended_candles("BTC-USD", "mark-prices", "5m", 2880)
btc_index_prices = get_extended_candles("BTC-USD", "index-prices", "5m", 2880)
```

### 5.5 Premium Index Berechnung

```python
def calculate_premium_index(
    mark_prices: List[Dict],
    index_prices: List[Dict],
) -> List[Dict]:
    """
    Berechnet Premium Index = (Mark - Index) / Index

    Positiver Premium → Longs zahlen Funding
    Negativer Premium → Shorts zahlen Funding
    """
    # Nach Timestamp indizieren
    mark_by_ts = {p["T"]: float(p["c"]) for p in mark_prices}
    index_by_ts = {p["T"]: float(p["c"]) for p in index_prices}

    premiums = []
    for ts in sorted(set(mark_by_ts.keys()) & set(index_by_ts.keys())):
        mark = mark_by_ts[ts]
        index = index_by_ts[ts]

        if index > 0:
            premium = (mark - index) / index
            premiums.append({
                "timestamp": ts,
                "mark_price": mark,
                "index_price": index,
                "premium_index": premium,
                "premium_bps": premium * 10000,  # In Basis Points
            })

    return premiums
```

---

## Teil 6: Vollständige API-Endpunkt-Referenz

### 6.1 Lighter REST API Endpunkte

#### Account-Endpunkte

| Methode | Pfad | Parameter | Beschreibung |
|---------|------|-----------|--------------|
| GET | `/api/v1/account` | `by` (index/l1_address), `value` | Account-Daten abrufen |
| GET | `/api/v1/accountsByL1Address` | `l1_address` | Alle Accounts einer L1-Adresse |
| GET | `/api/v1/accountLimits` | `account_index` | Rate-Limits für Account |
| GET | `/api/v1/accountMetadata` | `account_index` | Metadaten (Name, Beschreibung) |
| GET | `/api/v1/pnl` | `account_index`, `market_id` | PnL-Daten |
| GET | `/api/v1/l1Metadata` | `l1_address` | L1-Wallet-Metadaten |
| POST | `/api/v1/changeAccountTier` | Body: signed tx | Auf Premium wechseln |
| GET | `/api/v1/liquidations` | `account_index` | Liquidations-Historie |
| GET | `/api/v1/positionFunding` | `account_index`, `market_id` | Position Funding History |

#### Order-Endpunkte

| Methode | Pfad | Parameter | Beschreibung |
|---------|------|-----------|--------------|
| GET | `/api/v1/accountActiveOrders` | `account_index`, `market_id`, `auth` | Aktive Orders |
| GET | `/api/v1/accountInactiveOrders` | `account_index`, `market_id`, `auth` | Inaktive Orders |
| GET | `/api/v1/orderBooks` | `market_id` (optional, default 255=alle) | Orderbooks |
| GET | `/api/v1/orderBookDetails` | `market_id` | Detaillierte Orderbook-Daten |
| GET | `/api/v1/orderBookOrders` | `market_id`, `limit` | Orders im Orderbook |
| GET | `/api/v1/recentTrades` | `market_id`, `limit` | Letzte Trades |
| GET | `/api/v1/trades` | `market_id`, `limit` (1-100) | Trade-Historie |

#### Transaktions-Endpunkte

| Methode | Pfad | Parameter | Beschreibung |
|---------|------|-----------|--------------|
| POST | `/api/v1/sendTx` | Body: `{tx_type, tx_info, signature}` | Einzelne Transaktion |
| POST | `/api/v1/sendTxBatch` | Body: `{tx_types[], tx_infos[]}` | Batch (max 50) |
| GET | `/api/v1/tx` | `hash` | Transaktion nach Hash |
| GET | `/api/v1/txs` | `account_index`, `limit` | Transaktions-Historie |
| GET | `/api/v1/nextNonce` | `account_index`, `api_key_index` | Nächste Nonce |

#### Market Data Endpunkte

| Methode | Pfad | Parameter | Beschreibung |
|---------|------|-----------|--------------|
| GET | `/api/v1/candles` | `market_id`, `resolution`, `start_timestamp`, `end_timestamp`, `count_back` | OHLCV-Candles |
| GET | `/api/v1/fundings` | `market_id`, `resolution` (1h/1d), `start_timestamp`, `end_timestamp`, `count_back` | Funding-Historie |
| GET | `/api/v1/funding-rates` | - | Aktuelle Funding-Rates aller Märkte |
| GET | `/api/v1/assetDetails` | - | Asset-Informationen |
| GET | `/api/v1/exchangeStats` | - | Exchange-Statistiken |

#### Candles Resolution Values

| Resolution | Beschreibung |
|------------|--------------|
| `1m` | 1 Minute |
| `5m` | 5 Minuten |
| `15m` | 15 Minuten |
| `30m` | 30 Minuten |
| `1h` | 1 Stunde |
| `4h` | 4 Stunden |
| `12h` | 12 Stunden |
| `1d` | 1 Tag |
| `1w` | 1 Woche |

### 6.2 Extended REST API Endpunkte

#### Public Market Data

| Methode | Pfad | Parameter | Beschreibung |
|---------|------|-----------|--------------|
| GET | `/api/v1/info/markets` | `market` (optional, mehrere möglich) | Markt-Konfigurationen |
| GET | `/api/v1/info/markets/{market}/stats` | - | Markt-Statistiken |
| GET | `/api/v1/info/markets/{market}/orderbook` | `depth` | Orderbook-Snapshot |
| GET | `/api/v1/info/markets/{market}/trades` | `limit`, `startTime`, `endTime` | Öffentliche Trades |
| GET | `/api/v1/info/candles/{market}/{candleType}` | `interval`, `limit`, `endTime` | Candles (siehe unten) |
| GET | `/api/v1/info/{market}/funding` | `startTime`, `endTime`, `cursor`, `limit` | Funding-Historie |
| GET | `/api/v1/info/{market}/open-interests` | `startTime`, `endTime` | Open Interest Historie |

#### Candle Types für Extended

| candleType | Beschreibung |
|------------|--------------|
| `trades` | Normale OHLCV Candles |
| `mark-prices` | Mark Price Candles |
| `index-prices` | Index Price Candles |

#### Private Account Endpunkte (erfordern X-Api-Key Header)

| Methode | Pfad | Parameter | Beschreibung |
|---------|------|-----------|--------------|
| GET | `/api/v1/user/account/info` | - | Account-Details |
| GET | `/api/v1/user/balance` | - | Balance & Equity |
| GET | `/api/v1/user/positions` | `market` (optional) | Offene Positionen |
| GET | `/api/v1/user/positions/history` | `startTime`, `endTime`, `market` | Position History |
| GET | `/api/v1/user/leverage` | - | Leverage pro Markt |
| PATCH | `/api/v1/user/leverage` | Body: `{market, leverage}` | Leverage ändern |
| GET | `/api/v1/user/fees` | - | Fee-Rates |

#### Private Order Endpunkte

| Methode | Pfad | Parameter | Beschreibung |
|---------|------|-----------|--------------|
| GET | `/api/v1/user/orders` | `market`, `status`, `limit` | Offene Orders |
| GET | `/api/v1/user/orders/history` | `market`, `startTime`, `endTime` | Order History |
| GET | `/api/v1/user/orders/{id}` | - | Order nach Extended-ID |
| GET | `/api/v1/user/orders/external/{externalId}` | - | Order nach User-ID |
| POST | `/api/v1/user/order` | Body: Order-Objekt + Signatur | Order erstellen/ändern |
| DELETE | `/api/v1/user/order/{id}` | - | Order canceln |
| DELETE | `/api/v1/user/order/external/{externalId}` | - | Order canceln nach User-ID |
| POST | `/api/v1/user/order/mass-cancel` | Body: Filter | Mehrere Orders canceln |

#### Private Trade/Funding Endpunkte

| Methode | Pfad | Parameter | Beschreibung |
|---------|------|-----------|--------------|
| GET | `/api/v1/user/trades` | `market`, `startTime`, `endTime`, `limit` | Trade History |
| GET | `/api/v1/user/funding/history` | `market`, `startTime`, `endTime` | Funding Payments |
| GET | `/api/v1/user/pnl/history` | `startTime`, `endTime` | PnL History |
| GET | `/api/v1/user/equity/history` | `startTime`, `endTime` | Equity History |

### 6.3 WebSocket Channels (Lighter)

#### Public Channels (keine Auth erforderlich)

| Channel | Subscription | Update-Typ |
|---------|--------------|------------|
| Order Book | `order_book/{MARKET_INDEX}` | `update/order_book` |
| Market Stats | `market_stats/{MARKET_INDEX}` oder `market_stats/all` | `update/market_stats` |
| Trade | `trade/{MARKET_INDEX}` | `update/trade` |
| Pool Info | `pool_info/{ACCOUNT_ID}` | `update/pool_info` |

#### Private Channels (Auth-Token erforderlich)

| Channel | Subscription | Update-Typ | Beschreibung |
|---------|--------------|------------|--------------|
| Account All | `account_all/{ACCOUNT_ID}` | `update/account_all` | Alle Account-Updates |
| Account Market | `account_market/{MARKET_ID}/{ACCOUNT_ID}` | `update/account_market` | Market-spezifische Updates |
| Account Orders | `account_orders/{MARKET_INDEX}/{ACCOUNT_ID}` | `update/account_orders` | Order-Updates |
| Account All Positions | `account_all_positions/{ACCOUNT_ID}` | `update/account_all_positions` | Alle Positionen |
| Account All Assets | `account_all_assets/{ACCOUNT_ID}` | `update/account_all_assets` | Asset-Balances |
| Account All Trades | `account_all_trades/{ACCOUNT_ID}` | `update/account_all_trades` | Trade-Updates |
| User Stats | `user_stats/{ACCOUNT_ID}` | `update/user_stats` | Portfolio-Statistiken |
| Notification | `notification/{ACCOUNT_ID}` | `subscribed/notification` | Liquidation/ADL Alerts |
| Pool Data | `pool_data/{ACCOUNT_ID}` | `subscribed/pool_data` | Pool-Daten |

#### WebSocket Auth-Payload (Lighter)

```json
{
  "type": "subscribe",
  "channel": "account_all/{ACCOUNT_ID}",
  "auth": "{AUTH_TOKEN}"
}
```

Auth-Token-Format: `{expiry_unix}:{account_index}:{api_key_index}:{random_hex}`

### 6.4 WebSocket Streams (Extended)

#### Public Streams

| Stream | URL | Beschreibung |
|--------|-----|--------------|
| Order Book | `/stream.extended.exchange/v1/orderbook/{market}` | Level 2 Depth |
| Trades | `/stream.extended.exchange/v1/trades/{market}` | Live Trades |
| Mark Price | `/stream.extended.exchange/v1/prices/mark/{market}` | Mark Price Updates |
| Index Price | `/stream.extended.exchange/v1/prices/index/{market}` | Index Price Updates |
| Candles | `/stream.extended.exchange/v1/candles/{market}/{interval}` | OHLCV Stream |

#### Private Stream (erfordert Auth)

| Stream | URL | Beschreibung |
|--------|-----|--------------|
| Account Updates | `/stream.extended.exchange/v1/user` | Positions, Orders, Balance |

---

## Teil 7: Mark Price & Funding Rate Formeln

### 7.1 Lighter Funding-Rate-Formel

```
Premium Index (jede Minute):
premium_t = [Max(0, Impact Bid - Index) - Max(0, Index - Impact Ask)] / Index

Funding Rate (stündlich):
TWAP = Time-Weighted Average von premium_t (60 Samples/Stunde)
fundingRate = (TWAP / 8) + interestRateComponent

Rate-Capping: ±0.5% pro Stunde

Funding Payment:
funding = (-1) × position × mark_price × fundingRate
```

### 7.2 Extended Funding-Rate-Formel

```
Premium Index (alle 5 Sekunden):
Premium_Index = [Max(0, Impact Bid - Index) - Max(0, Index - Impact Ask)] / Index

Average Premium (720 Samples = 1 Stunde):
Average_Premium = TWAP(Premium_Index_1, ..., Premium_Index_720)

Funding Rate:
fundingRate = (Average_Premium + clamp(InterestRate - Average_Premium, -0.05%, +0.05%)) / 8

Rate-Capping: Markt-abhängig (Gruppe 1: 1%, Gruppe 2: 2%, Gruppe 3: 3%)

Impact Notional:
- Gruppe 1: $10,000
- Gruppe 2: $5,000
- Gruppe 3: $2,500

Funding Payment:
funding = Position_Size × Mark_Price × (-fundingRate)
```

### 7.3 Impact Price Berechnung

```python
def calculate_impact_price(
    orderbook: dict,
    impact_notional: float,
    side: str,  # "bid" oder "ask"
) -> float:
    """
    Impact Price = VWAP für Impact Notional auf einer Seite des Orderbooks.

    Extended Impact Notional:
    - Gruppe 1 (BTC, ETH): $10,000
    - Gruppe 2: $5,000
    - Gruppe 3: $2,500
    """
    levels = orderbook["bids"] if side == "bid" else orderbook["asks"]

    remaining_notional = impact_notional
    weighted_sum = 0.0
    total_size = 0.0

    for level in levels:
        price = float(level["price"])
        size = float(level["size"])
        level_notional = price * size

        if level_notional >= remaining_notional:
            # Partial fill
            fill_size = remaining_notional / price
            weighted_sum += price * fill_size
            total_size += fill_size
            break
        else:
            weighted_sum += price * size
            total_size += size
            remaining_notional -= level_notional

    return weighted_sum / total_size if total_size > 0 else 0.0
```

---

## Teil 8: Vollständige API-Parameter-Typen

### 8.1 Lighter API - Detaillierte Typisierung

#### GET /api/v1/candles

| Parameter | Typ | Required | Range/Enum | Beschreibung |
|-----------|-----|----------|------------|--------------|
| `market_id` | `int16` | Yes | 0-255 | Market-Index |
| `resolution` | `string` | Yes | `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `12h`, `1d`, `1w` | Zeitintervall |
| `start_timestamp` | `int64` | Yes | 0-5000000000000 | Unix-Timestamp (ms) |
| `end_timestamp` | `int64` | Yes | 0-5000000000000 | Unix-Timestamp (ms) |
| `count_back` | `int64` | Yes | 1-1000 | Anzahl Candles |
| `set_timestamp_to_end` | `boolean` | No | `true`/`false` | Default: `false` |

**Response-Typen:**
```json
{
  "code": "int16",
  "candles": [{
    "open": "string (Decimal)",
    "high": "string (Decimal)",
    "low": "string (Decimal)",
    "close": "string (Decimal)",
    "volume": "string (Decimal)",
    "timestamp": "int64"
  }]
}
```

#### GET /api/v1/fundings

| Parameter | Typ | Required | Range/Enum | Beschreibung |
|-----------|-----|----------|------------|--------------|
| `market_id` | `uint8` | Yes | 0-255 | Market-Index |
| `resolution` | `string` | Yes | `1h`, `1d` | Funding-Intervall |
| `start_timestamp` | `int64` | Yes | 0-5000000000000 | Unix-Timestamp (ms) |
| `end_timestamp` | `int64` | Yes | 0-5000000000000 | Unix-Timestamp (ms) |
| `count_back` | `int64` | Yes | 1-1000 | Anzahl Datenpunkte |

**Response-Typen:**
```json
{
  "code": "int16",
  "fundings": [{
    "funding_rate": "string (Decimal, z.B. '0.0001')",
    "mark_price": "string (Decimal)",
    "timestamp": "int64"
  }]
}
```

#### GET /api/v1/accountActiveOrders

| Parameter | Typ | Required | Location | Beschreibung |
|-----------|-----|----------|----------|--------------|
| `authorization` | `string` | No | Header | Bearer Token |
| `account_index` | `int64` | Yes | Query | Account-ID |
| `market_id` | `uint8` | Yes | Query | Market-Index |
| `auth` | `string` | No | Query | Alternative Auth |

#### GET /api/v1/account

| Parameter | Typ | Required | Enum | Beschreibung |
|-----------|-----|----------|------|--------------|
| `by` | `string` | Yes | `index`, `l1_address` | Suchfeld |
| `value` | `string` | Yes | - | Suchwert |

### 8.2 Extended API - Detaillierte Typisierung

#### GET /v1/funding-history/{market}

| Parameter | Typ | Required | Location | Beschreibung |
|-----------|-----|----------|----------|--------------|
| `market` | `string` | Yes | Path | z.B. `BTC-USD` |
| `startTime` | `number` | Yes | Query | Epoch (ms) |
| `endTime` | `number` | Yes | Query | Epoch (ms) |
| `cursor` | `number` | No | Query | Pagination-Offset |
| `limit` | `number` | No | Query | Max Items (default: 100) |

**Response-Typen:**
```json
{
  "status": "string ('OK' | 'ERROR')",
  "data": [{
    "m": "string (market)",
    "T": "number (timestamp)",
    "f": "string (funding_rate, Decimal)"
  }],
  "pagination": {
    "cursor": "number (int64)",
    "count": "number"
  }
}
```

#### GET /v1/trades/{market}

| Parameter | Typ | Required | Location | Beschreibung |
|-----------|-----|----------|----------|--------------|
| `market` | `string` | Yes | Path | Market-Name |

**Response-Felder:**
```json
{
  "i": "number (int64, trade_id)",
  "m": "string (market)",
  "S": "string ('BUY' | 'SELL')",
  "tT": "string ('TRADE' | 'LIQUIDATION')",
  "T": "number (int64, timestamp_ms)",
  "p": "string (Decimal, price)",
  "q": "string (Decimal, quantity)"
}
```

### 8.3 Extended SDK - Pydantic Models

```python
from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel

class OpenOrderModel(BaseModel):
    id: int                                    # int64
    account_id: int                            # int64
    external_id: str                           # UUID string
    market: str                                # "BTC-USD"
    type: OrderType                            # Enum
    side: OrderSide                            # "BUY" | "SELL"
    status: OrderStatus                        # Enum
    status_reason: Optional[OrderStatusReason] # Enum, nullable
    price: Decimal                             # string -> Decimal
    average_price: Optional[Decimal]           # nullable
    qty: Decimal                               # Original quantity
    filled_qty: Optional[Decimal]              # Cumulative filled
    reduce_only: bool
    post_only: bool
    created_time: int                          # int64 timestamp
    updated_time: int                          # int64 timestamp
    expiry_time: Optional[int]                 # int64, nullable

class PositionModel(BaseModel):
    id: int                                    # int64
    account_id: int                            # int64
    market: str
    side: PositionSide                         # "LONG" | "SHORT"
    leverage: Decimal
    size: Decimal                              # Position size
    value: Decimal                             # Notional value
    open_price: Decimal                        # Entry price (VWAP)
    mark_price: Decimal                        # Current mark
    liquidation_price: Optional[Decimal]       # nullable
    unrealised_pnl: Decimal
    realised_pnl: Decimal
    tp_price: Optional[Decimal]                # Take-Profit
    sl_price: Optional[Decimal]                # Stop-Loss
    adl: Optional[int]                         # ADL ranking (1-5)
    created_at: int                            # int64
    updated_at: int                            # int64
```

---

## Teil 9: Self-Trade Prevention (STP)

### 9.1 Lighter STP-Mechanismus

**Quelle:** docs.lighter.xyz/perpetual-futures/self-trade-prevention

> "Lighter imposes a self-trade prevention mechanism. Trades between the same account cancel the resting order (maker) instead of executing the respective trade."

**Lighter STP-Verhalten:**
- **Modus:** Cancel Maker (CM) - EINZIGER Modus
- **Automatisch:** Immer aktiv, keine API-Parameter erforderlich
- **Verhalten:** Wenn ein Taker-Order gegen einen eigenen Maker-Order matchen würde:
  1. Der Maker-Order wird gecancelt
  2. Der Taker-Order wird gegen nächsten verfügbaren Maker gematcht
  3. Keine Gebühren für gecancelten Self-Trade

**Keine Konfiguration möglich:** Lighter bietet KEINEN `stp` Parameter in der Order-API.

### 9.2 Extended/X10 STP-Mechanismus

Extended verwendet ein konfigurierbares STP-System ähnlich Binance/CoinEx.

**STP-Modi (über API-Parameter `stp_mode`):**

| Modus | API-Wert | Verhalten |
|-------|----------|-----------|
| No STP | `NO` | Self-Trades erlaubt |
| Cancel Taker | `CT` | Taker-Order wird gecancelt |
| Cancel Maker | `CM` | Maker-Order wird gecancelt |
| Cancel Both | `BOTH` | Beide Orders werden gecancelt |

**API-Payload Beispiel:**
```json
{
  "market": "BTC-USD",
  "side": "BUY",
  "type": "LIMIT",
  "price": "65000",
  "quantity": "0.1",
  "stp_mode": "CT"
}
```

**Anwendungsregeln:**
1. STP greift bei Orders vom selben Account
2. Bei Match zwischen Taker und eigenem Maker:
   - `CT`: Taker gecancelt, Maker bleibt
   - `CM`: Maker gecancelt, Taker wird weiter gematcht
   - `BOTH`: Beide gecancelt
3. FOK-Orders: STP wird NICHT angewendet
4. IOC/Post-Only: STP UND IOC/PO-Bedingungen müssen erfüllt sein

---

## Teil 10: Liquidation & Insurance Fund

### 10.1 Lighter Liquidation System

**Quelle:** docs.lighter.xyz, assets.lighter.xyz/whitepaper.pdf

#### Liquidation-Trigger
```
Liquidation wenn: Margin Ratio < MMR (Maintenance Margin Ratio)
Margin Ratio = (Equity) / (Position Notional)
```

#### Liquidation-Prozess
1. **Partielle Liquidation (Zero Price):** Position wird in kleinen Schritten reduziert
2. **Volle Liquidation:** Wenn partielle Liquidation nicht ausreicht
3. **LLP Übernahme:** Lighter Liquidity Pool übernimmt Position
4. **ADL (Auto-Deleveraging):** Letztes Mittel, nur wenn LLP erschöpft

#### Lighter Liquidity Pool (LLP)
- **Funktion:** Insurance Fund + Liquidationspartner
- **Gebühr:** 1% Liquidation Fee
- **Trading Fees:** 0% für Standard-Accounts (Zero Fee Model)
- **Finanzierung:** Liquidation Fees, Protocol Revenue

#### Liquidation Fee Berechnung
```python
def calculate_liquidation_fee(position_notional: Decimal) -> Decimal:
    """
    Lighter: 1% flat liquidation fee
    """
    LIQUIDATION_FEE_RATE = Decimal("0.01")  # 1%
    return position_notional * LIQUIDATION_FEE_RATE
```

### 10.2 Extended Liquidation System

**Quelle:** docs.extended.exchange/perpetual-futures/margin-schedule

#### Liquidation-Trigger
```
Liquidation wenn: Account Margin < MMR × Position Notional
Account Margin = Equity - Initial Margin (für andere Positionen)
```

#### Margin-Typen
| Margin-Typ | Abkürzung | Beschreibung |
|------------|-----------|--------------|
| Initial Margin | IMR | Zum Eröffnen erforderlich |
| Maintenance Margin | MMR | Zum Halten erforderlich |
| Close-out Margin | CMR | Liquidationsschwelle |

#### Insurance Fund
- **Struktur:** Separater Pool pro Asset
- **Finanzierung:** Liquidation Fees + Trading Fee Anteil
- **Verwendung:** Deckung von Liquidationsverlusten vor ADL

---

## Teil 11: Risk-Tier-Tabellen (Margin Schedule)

### 11.1 Extended Margin Schedule

**Gruppe 1: BTC, ETH (Majors)**

| Position Bracket (USD) | Max Leverage | IMR | MMR |
|------------------------|--------------|-----|-----|
| 0 - 100,000 | 50x | 2.0% | 1.2% |
| 100,000 - 500,000 | 25x | 4.0% | 2.4% |
| 500,000 - 1,000,000 | 20x | 5.0% | 3.0% |
| 1,000,000 - 5,000,000 | 10x | 10.0% | 6.0% |
| 5,000,000 - 10,000,000 | 5x | 20.0% | 12.0% |
| > 10,000,000 | 2x | 50.0% | 30.0% |

**Gruppe 2: SOL, XRP, DOGE, etc.**

| Position Bracket (USD) | Max Leverage | IMR | MMR |
|------------------------|--------------|-----|-----|
| 0 - 50,000 | 25x | 4.0% | 2.4% |
| 50,000 - 200,000 | 15x | 6.67% | 4.0% |
| 200,000 - 500,000 | 10x | 10.0% | 6.0% |
| 500,000 - 1,000,000 | 5x | 20.0% | 12.0% |
| > 1,000,000 | 2x | 50.0% | 30.0% |

**Gruppe 3: Altcoins (AVAX, LINK, etc.)**

| Position Bracket (USD) | Max Leverage | IMR | MMR |
|------------------------|--------------|-----|-----|
| 0 - 25,000 | 20x | 5.0% | 3.0% |
| 25,000 - 100,000 | 10x | 10.0% | 6.0% |
| 100,000 - 250,000 | 5x | 20.0% | 12.0% |
| > 250,000 | 2x | 50.0% | 30.0% |

**Gruppe 4-6: Small Caps / Meme Coins**

| Gruppe | Max Leverage | Max Position |
|--------|--------------|--------------|
| Gruppe 4 | 15x | $500,000 |
| Gruppe 5 | 10x | $250,000 |
| Gruppe 6 | 5x | $100,000 |

### 11.2 Lighter Margin Schedule

**Hinweis:** Lighter verwendet ein dynamisches Margin-System basierend auf Market Risk.

| Market-Typ | Beispiel | Max Leverage | IMR | MMR |
|------------|----------|--------------|-----|-----|
| Tier 1 | BTC-USD | 50x | 2% | 1% |
| Tier 2 | ETH-USD | 30x | 3.33% | 1.67% |
| Tier 3 | SOL-USD | 20x | 5% | 2.5% |
| Tier 4 | Altcoins | 10x | 10% | 5% |

---

## Teil 12: Infrastructure & Konfiguration

### 12.1 Extended Starknet Konfiguration

**Aus GitHub SDK (starknet branch):**

```python
# Testnet Configuration
TESTNET_CONFIG = {
    "rpc_url": "https://rpc.sepolia.org",
    "api_base": "https://api.starknet.sepolia.extended.exchange/api/v1",
    "stream_url": "wss://api.starknet.sepolia.extended.exchange/stream.extended.exchange/v1",
    "onboarding_url": "https://api.starknet.sepolia.extended.exchange",
    "chain_id": "SN_SEPOLIA",
    "signing_domain": "starknet.sepolia.extended.exchange",
    "collateral_decimals": 6,
    "collateral_asset_id": "0x1",
    "collateral_contract": "0x31857064564ed0ff978e687456963cba09c2c6985d8f9300a1de4962fafa054",
}

# Mainnet Configuration
MAINNET_CONFIG = {
    "api_base": "https://api.starknet.extended.exchange/api/v1",
    "stream_url": "wss://api.starknet.extended.exchange/stream.extended.exchange/v1",
    "onboarding_url": "https://api.starknet.extended.exchange",
    "chain_id": "SN_MAIN",
    "signing_domain": "extended.exchange",
    "collateral_decimals": 6,
    "collateral_asset_id": "0x1",
}

# Starknet Typed Data Domain
STARKNET_DOMAIN = {
    "name": "Perpetuals",
    "version": "v0",
    "revision": 1,
    "chainId": "0x1",  # On-chain ID
}
```

### 12.2 Extended Asset Resolution System

```python
from decimal import Decimal
from dataclasses import dataclass

@dataclass
class Asset:
    id: str
    name: str
    precision: int                    # Display decimals
    active: bool
    is_collateral: bool
    settlement_resolution: int        # STARK scaling
    l1_resolution: int               # L1 scaling (collateral only)
    external_settlement_asset_id: str
    external_l1_asset_id: str

    def convert_human_readable_to_stark_quantity(
        self,
        amount: Decimal
    ) -> int:
        """Skaliert menschenlesbare Dezimalwerte auf STARK-Format."""
        return int(amount * self.settlement_resolution)

    def convert_stark_to_internal_quantity(
        self,
        stark_qty: int
    ) -> Decimal:
        """Konvertiert STARK-Quantities zurück zu internen Dezimalwerten."""
        return Decimal(stark_qty) / Decimal(self.settlement_resolution)

# Typische Collateral-Konfiguration (USDC)
USDC_ASSET = Asset(
    id="0x1",
    name="USDC",
    precision=6,
    active=True,
    is_collateral=True,
    settlement_resolution=10**18,     # Starknet-Standard
    l1_resolution=10**6,              # USDC L1 decimals
    external_settlement_asset_id="...",
    external_l1_asset_id="...",
)
```

### 12.3 L3 Orderbook-Daten

**Wichtige Erkenntnis:** Weder Lighter noch Extended bieten öffentliche L3 (Market-by-Order) Daten.

| Exchange | L2 Data | L3 Data | WebSocket |
|----------|---------|---------|-----------|
| Lighter | ✅ Aggregiert | ❌ Nicht verfügbar | ✅ Real-time |
| Extended | ✅ Aggregiert | ❌ Nicht verfügbar | ✅ Real-time |

**L3 Data in Crypto:**
- Nur wenige Exchanges bieten L3: Coinbase, Bitso, Kraken (authentifiziert)
- L3 enthält: order_id, exact size, price, timestamp, queue position
- Volume: 50-500 GB/Tag pro aktiv gehandeltes Symbol

**Lighter WebSocket Orderbook (L2):**
```json
{
  "channel": "order_book:{MARKET_INDEX}",
  "order_book": {
    "asks": [{"price": "65000.50", "size": "1.5"}],
    "bids": [{"price": "64999.50", "size": "2.3"}],
    "offset": 12345,
    "nonce": 67890
  }
}
```

---

## Teil 13: 30-Tage Funding-Historie (Lückenlose Abfrage)

### 13.1 Lighter: Vollständige Funding-Abfrage

```python
import requests
from datetime import datetime, timedelta
from typing import List, Dict

def fetch_lighter_funding_30d(
    market_id: int,
    base_url: str = "https://api.lighter.xyz",
) -> List[Dict]:
    """
    Holt 30 Tage Funding-Historie für ein Market.

    WICHTIG:
    - Resolution "1h" = 720 Datenpunkte für 30 Tage
    - Lighter applied Funding STÜNDLICH (nicht 8h!)
    - count_back kann max 1000 sein
    """
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=30)).timestamp() * 1000)

    params = {
        "market_id": market_id,
        "resolution": "1h",           # MUSS "1h" sein für korrektes Z-Score
        "start_timestamp": start_time,
        "end_timestamp": end_time,
        "count_back": 720,            # 30 Tage × 24 Stunden
    }

    response = requests.get(
        f"{base_url}/api/v1/fundings",
        params=params,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    return data.get("fundings", [])


def validate_funding_completeness(
    fundings: List[Dict],
    expected_hours: int = 720,
) -> Dict:
    """
    Prüft Funding-Daten auf Lücken.

    Returns:
        {
            "complete": bool,
            "expected": int,
            "actual": int,
            "missing_hours": List[datetime],
            "coverage_pct": float,
        }
    """
    if not fundings:
        return {"complete": False, "expected": expected_hours, "actual": 0}

    timestamps = sorted([f["timestamp"] for f in fundings])

    # Erwartete Stunden-Slots
    start_ts = timestamps[0]
    end_ts = timestamps[-1]
    expected_slots = set()

    current = start_ts
    while current <= end_ts:
        expected_slots.add(current)
        current += 3600 * 1000  # 1 Stunde in ms

    actual_slots = set(timestamps)
    missing = expected_slots - actual_slots

    return {
        "complete": len(missing) == 0,
        "expected": len(expected_slots),
        "actual": len(actual_slots),
        "missing_hours": [
            datetime.fromtimestamp(ts/1000) for ts in sorted(missing)
        ],
        "coverage_pct": len(actual_slots) / len(expected_slots) * 100,
    }
```

### 13.2 Extended: Vollständige Funding-Abfrage mit Pagination

```python
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional

def fetch_extended_funding_30d(
    market: str,
    base_url: str = "https://api.starknet.extended.exchange/api/v1",
) -> List[Dict]:
    """
    Holt 30 Tage Funding-Historie mit automatischer Pagination.

    Extended API:
    - Pagination via cursor
    - Max 100 items per request (default)
    - startTime/endTime in epoch milliseconds
    """
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=30)).timestamp() * 1000)

    all_fundings = []
    cursor: Optional[int] = None

    while True:
        params = {
            "startTime": start_time,
            "endTime": end_time,
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor

        response = requests.get(
            f"{base_url}/funding-history/{market}",
            params=params,
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()

        if data["status"] != "OK":
            raise ValueError(f"API Error: {data.get('error', {})}")

        fundings = data.get("data", [])
        all_fundings.extend(fundings)

        # Pagination
        pagination = data.get("pagination", {})
        if pagination.get("count", 0) < 100:
            # Keine weiteren Seiten
            break

        cursor = pagination.get("cursor")
        if not cursor:
            break

    return all_fundings


def convert_extended_to_standard_format(
    fundings: List[Dict],
) -> List[Dict]:
    """
    Konvertiert Extended-Format zu Standard-Format.

    Extended Response: {"m": "BTC-USD", "T": 1701563440, "f": "0.001"}
    Standard Format: {"market": "...", "timestamp": ..., "funding_rate": ...}
    """
    return [
        {
            "market": f["m"],
            "timestamp": f["T"] * 1000,  # Sekunden → Millisekunden
            "funding_rate": float(f["f"]),
        }
        for f in fundings
    ]
```

### 13.3 Z-Score Berechnung für Funding Arbitrage

```python
import numpy as np
from scipy import stats
from typing import List, Dict, Tuple

def calculate_funding_zscore(
    lighter_fundings: List[Dict],
    extended_fundings: List[Dict],
    lookback_hours: int = 168,  # 7 Tage Default
) -> Dict:
    """
    Berechnet Z-Score für Funding-Differenz zwischen Lighter und Extended.

    Z-Score > 2.0: Funding-Spread statistisch signifikant hoch
    Z-Score < -2.0: Funding-Spread statistisch signifikant niedrig

    Returns:
        {
            "zscore": float,
            "spread_mean": float,
            "spread_std": float,
            "current_spread": float,
            "lighter_rate": float,
            "extended_rate": float,
            "signal": str,  # "LONG_SPREAD", "SHORT_SPREAD", "NEUTRAL"
        }
    """
    # Alignment auf gemeinsame Timestamps
    lighter_map = {f["timestamp"]: f["funding_rate"] for f in lighter_fundings}
    extended_map = {f["timestamp"]: f["funding_rate"] for f in extended_fundings}

    common_ts = sorted(
        set(lighter_map.keys()) & set(extended_map.keys())
    )[-lookback_hours:]

    if len(common_ts) < lookback_hours * 0.9:
        raise ValueError(
            f"Insufficient data overlap: {len(common_ts)}/{lookback_hours}"
        )

    spreads = [
        lighter_map[ts] - extended_map[ts]
        for ts in common_ts
    ]

    spread_mean = np.mean(spreads)
    spread_std = np.std(spreads)
    current_spread = spreads[-1]

    # Z-Score
    zscore = (current_spread - spread_mean) / spread_std if spread_std > 0 else 0

    # Signal-Generierung
    if zscore > 2.0:
        signal = "LONG_SPREAD"   # Long Lighter, Short Extended
    elif zscore < -2.0:
        signal = "SHORT_SPREAD"  # Short Lighter, Long Extended
    else:
        signal = "NEUTRAL"

    return {
        "zscore": round(zscore, 4),
        "spread_mean": round(spread_mean, 6),
        "spread_std": round(spread_std, 6),
        "current_spread": round(current_spread, 6),
        "lighter_rate": lighter_map[common_ts[-1]],
        "extended_rate": extended_map[common_ts[-1]],
        "signal": signal,
        "percentile": round(stats.norm.cdf(zscore) * 100, 2),
    }
```

---

## Quellen

### Lighter.xyz
- API Docs: https://apidocs.lighter.xyz/
- General Docs: https://docs.lighter.xyz/
- Python SDK: https://github.com/elliottech/lighter-python
- Go SDK: https://github.com/elliottech/lighter-go
- Whitepaper: https://assets.lighter.xyz/whitepaper.pdf
- L2BEAT: https://l2beat.com/scaling/projects/lighter

### Extended Exchange
- Docs: https://docs.extended.exchange/
- API Docs: https://api.docs.extended.exchange/
- Legacy API Docs: https://x101.docs.apiary.io/
- Python SDK: https://github.com/x10xchange/python_sdk
- Examples: https://github.com/x10xchange/examples
- Starknet Domain: https://docs.starkware.co/

---

## Confidence Levels

| Sektion | Confidence | Notizen |
|---------|------------|---------|
| Lighter API | 95% | Direkt aus offizieller Dokumentation |
| Lighter SDK | 95% | GitHub Repository verifiziert |
| Lighter Architecture | 90% | Whitepaper + L2Beat |
| Extended API | 90% | Apiary + GitHub |
| Extended SDK | 95% | GitHub Repository verifiziert (starknet branch) |
| Funding Mechanismen | 95% | Beide Docs bestätigen stündliche Anwendung |
| Smart Contracts | 85% | Indirekte Quellen |
| API Parameter-Typen | 95% | Direkt aus API-Referenz extrahiert |
| STP-Mechanismen | 90% | Lighter Docs + Exchange-Vergleich |
| Liquidation/Insurance | 85% | Docs + Whitepaper |
| Risk-Tier-Tabellen | 90% | Extended Margin Schedule Docs |
| Starknet Config | 95% | GitHub SDK verifiziert |
| L3 Orderbook | 100% | Bestätigt: Nicht verfügbar bei beiden |
| Funding Z-Score | 90% | Implementierung basiert auf Standardmethodik |

---

## Zusammenfassung der Recherche

Diese Wissensdatenbank enthält **13 Teile** mit umfassenden technischen Details:

1. **Architektur**: ZK-Rollup (Lighter) vs StarkEx→Starknet (Extended)
2. **API-Grundlagen**: REST + WebSocket Endpunkte
3. **Order-Management**: Alle Order-Typen, Time-in-Force, Reduce-Only
4. **Code-Level Details**: ctypes, Starknet Signing, Error Codes
5. **Historische Daten**: Candles, Funding History mit Pagination
6. **Vollständige API-Referenz**: Alle Endpunkte beider Exchanges
7. **Mark Price & Funding Formeln**: Premium Index, TWAP, Impact Price
8. **API Parameter-Typen**: int64, string, boolean mit Ranges
9. **Self-Trade Prevention**: Lighter (CM-only) vs Extended (konfigurierbar)
10. **Liquidation & Insurance Fund**: LLP, Fees, ADL
11. **Risk-Tier-Tabellen**: 6 Gruppen mit Position Brackets
12. **Infrastructure**: Starknet Config, Asset Resolution, L3 Status
13. **30-Tage Funding-Historie**: Lückenlose Abfrage + Z-Score

**Kritische Erkenntnisse für Arbitrage-Bot:**
- Beide Exchanges: Funding STÜNDLICH (nicht 8h!)
- Lighter: 0% Trading Fees, 1% Liquidation Fee
- Extended: 6 Margin-Gruppen mit unterschiedlichen Limits
- Kein L3 Orderbook bei beiden Exchanges verfügbar
- Z-Score-Berechnung erfordert timestamp-alignment

---

## Teil 14: Operativer Betrieb & DevOps

### 14.1 Sequencer & Transaction Ordering

#### Lighter: FIFO + Price-Time Priority

**Quelle:** bitcoin.com, docs.lighter.xyz, assets.lighter.xyz/whitepaper.pdf

```
Transaction Flow:
1. User signiert Transaktion → Sequencer
2. Sequencer ordnet FIFO (First-In-First-Out)
3. Matching Engine: Price-Time Priority
4. ZK-Proof generiert für jedes Match
5. Proof-Aggregation → Ethereum Settlement
```

**Kritische Details:**
- **Soft Finality:** < 10ms (API-Response)
- **Hard Finality:** Nach ZK-Proof auf Ethereum (~13 Minuten)
- **Keine Priority Gas Auctions:** Sequencer ist zentralisiert, FIFO garantiert
- **Verifiable Matching:** SNARKs beweisen korrekte Price-Time Priority
- **Throughput:** 10.000+ Orders/Sekunde

**Sequencer Fail-Safe (Desert Mode/Escape Hatch):**
```
Wenn Sequencer > X Minuten nicht antwortet:
1. User kann Priority Request auf Ethereum L1 submitten
2. Smart Contract friert Exchange bei Nicht-Bearbeitung
3. User kann mit Data Blobs eigenen State rekonstruieren
4. Direkter Withdrawal auf L1 möglich
```

#### Extended: StarkEx/Starknet Sequencer

**Starknet 2025 Update:**
- **FIFO ist ABGESCHAFFT** auf Starknet (November 2025)
- **Neues Modell:** Transactions priorisiert nach Tips (EIP-1559 style)
- **Block Times:** 4 Sekunden (vorher 30 Sekunden)
- **Pre-confirmations:** ~500ms Latenz
- **Finality:** Abhängig von Starknet L1 Settlement (~13 Minuten)

### 14.2 Finality & Proof-Generierung unter Last

#### Lighter ZK-Proof Performance

**Bekannte Incidents:**

**Oktober 2025 Outage (PANews Report):**
```
Datum: 11. Oktober 2025, 00:17-05:08 Beijing Time
Ursache: Transaction-Surge 16x normal (65,638/min vs 4,005/min)
Auswirkung:
- 3 Batches verloren (ab Batch #55661)
- Prover konnte nicht mit Sequencer mithalten
- LLP (Insurance Fund) Verluste
```

**Dezember 2025 Withdrawal-Disruption:**
```
Ursache: Processing Lag zwischen Prover und Sequencer
Symptom: Last committed block 9+ Stunden alt
Auswirkung: Withdrawals verzögert/blockiert
```

**Lessons Learned:**
- **Prover-Kapazität:** Kann bei 16x Load nicht mithalten
- **Keine API-Metrik:** Kein öffentlicher Sequencer-Lag-Indikator
- **Monitoring:** On-chain Block-Explorer ist einzige Datenquelle

#### Extended/Starknet Finality

```
Typische ZK-Rollup Finality:
- L2 Mempool Latenz: ~2.5 Sekunden (Median)
- Miniblock Inclusion: Nach Sequencer-Verarbeitung
- L1 Batch Settlement: 15-60 Sekunden (nach Proof)
- Ethereum Finality: ~13 Minuten
```

### 14.3 WebSocket & Netzwerk-Konfiguration

#### Lighter WebSocket

| Parameter | Wert | Quelle |
|-----------|------|--------|
| Mainnet URL | `wss://mainnet.zklighter.elliot.ai/stream` | API Docs |
| Testnet URL | `wss://testnet.zklighter.elliot.ai/stream` | API Docs |
| Ping/Pong Intervall | **NICHT DOKUMENTIERT** | - |
| Connection Timeout | **NICHT DOKUMENTIERT** | - |
| Max Subscriptions | **NICHT DOKUMENTIERT** | - |

**Empfehlung für Bot:**
```python
# Standard WebSocket Keepalive (Industry Default)
PING_INTERVAL = 20  # Sekunden
PING_TIMEOUT = 20   # Sekunden
RECONNECT_DELAY = [1, 2, 4, 8, 16, 30]  # Exponential Backoff

async def websocket_keepalive(ws):
    while True:
        try:
            await asyncio.wait_for(
                ws.ping(),
                timeout=PING_TIMEOUT
            )
            await asyncio.sleep(PING_INTERVAL)
        except asyncio.TimeoutError:
            logger.warning("WebSocket ping timeout - reconnecting")
            raise ConnectionClosed()
```

#### Extended WebSocket

| Parameter | Wert | Quelle |
|-----------|------|--------|
| Mainnet URL | `wss://api.starknet.extended.exchange/stream.extended.exchange/v1` | SDK |
| Testnet URL | `wss://api.starknet.sepolia.extended.exchange/stream.extended.exchange/v1` | SDK |
| Orderbook Push | 100ms | API Docs |
| Ping/Pong | **NICHT DOKUMENTIERT** | - |

### 14.4 Rate Limits & IP-Restrictions

#### Extended Rate Limits

| Tier | Limit | Scope |
|------|-------|-------|
| Standard | 1,000 req/min | Alle Endpoints |
| Market Maker | 60,000 req/5min | Erhöhtes Limit |
| Rate-Limit Response | HTTP 429 | - |

**Keine IP-Whitelisting-Anforderung:** Authentifizierung nur via `X-Api-Key` Header.

#### Lighter Rate Limits

**NICHT ÖFFENTLICH DOKUMENTIERT**

Bekannte Einschränkungen aus Terms of Service:
- "Unauthorized access to the API" verboten
- "Price manipulation or market interference" verboten
- "Any activity deemed suspicious" kann zu Restrictions führen

### 14.5 Price Bands & Fat-Finger Protection

#### Lighter Fat-Finger Check

**Quelle:** docs.lighter.xyz/perpetual-futures/orders-and-matching

```python
# Fat Finger Prevention Check (Lighter)
def validate_limit_order_price(
    order_side: str,
    limit_price: Decimal,
    mark_price: Decimal,
    best_bid: Optional[Decimal],
    best_ask: Optional[Decimal],
) -> bool:
    """
    Lighter: 5% Abweichung vom Referenzpreis erlaubt.

    Ask Order: price >= max(MarkPrice, bestBid) × 0.95
    Bid Order: price <= min(MarkPrice, bestAsk) × 1.05
    """
    if order_side == "ASK":
        reference = max(mark_price, best_bid) if best_bid else mark_price
        return limit_price >= reference * Decimal("0.95")
    else:  # BID
        reference = min(mark_price, best_ask) if best_ask else mark_price
        return limit_price <= reference * Decimal("1.05")
```

#### Extended Price Caps

**Aus API Response (tradingConfig):**

```json
{
  "tradingConfig": {
    "limitPriceCap": "0.05",    // +5% vom Mark Price
    "limitPriceFloor": "0.05"   // -5% vom Mark Price
  }
}
```

**Berechnung:**
```
Max Buy Price = Mark Price × (1 + limitPriceCap)
Min Sell Price = Mark Price × (1 - limitPriceFloor)
```

### 14.6 Maintenance & Status Monitoring

#### Extended Market Status Flags

**API Response bei Maintenance:**

```json
{
  "status": "ERROR",
  "error": {
    "code": "REDUCE_ONLY_MODE",
    "message": "Exchange is in reduce-only mode, only reduce-only orders are allowed"
  }
}
```

**Market Status Enum:**

| Status | Beschreibung | Orders erlaubt |
|--------|--------------|----------------|
| `ACTIVE` | Normal | Alle |
| `REDUCE_ONLY` | Nur Position-Reduktion | Reduce-Only |
| `POST_ONLY_MODE` | Nur Maker-Orders | Post-Only |
| `TRADING_OFF_MODE` | Trading deaktiviert | Keine |
| `DISABLED` | Komplett deaktiviert | Keine |
| `PRELISTED` | Pre-Launch | Keine |
| `DELISTED` | Delisted | Keine |

#### Lighter Status Detection

**Keine offizielle Status-Page gefunden.**

**Monitoring-Empfehlung:**
```python
def check_lighter_health() -> dict:
    """
    Inoffizielle Health-Checks für Lighter.
    """
    checks = {}

    # 1. API Erreichbarkeit
    try:
        resp = requests.get(
            "https://api.lighter.xyz/api/v1/exchange",
            timeout=5
        )
        checks["api_reachable"] = resp.status_code == 200
    except:
        checks["api_reachable"] = False

    # 2. Block-Explorer für Sequencer-Health
    # Prüfe letzten committed Block-Timestamp
    # Wenn > 5 Minuten alt: WARNUNG

    # 3. WebSocket Connection Test
    # 4. Order-Placement Test (Dry-Run)

    return checks
```

### 14.7 Undocumented Constraints (Hidden Limits)

| Exchange | Constraint | Beobachtung | Quelle |
|----------|------------|-------------|--------|
| Lighter | Sequencer-Kapazität | 16x Normal-Load führt zu Ausfällen | PANews Oct 2025 |
| Lighter | Prover-Lag | Keine API-Metrik verfügbar | On-chain Analysis |
| Lighter | WebSocket Limits | Nicht dokumentiert, vermutlich Standard 20s Ping | - |
| Extended | Order-to-Trade Ratio | Nicht dokumentiert (anders als Binance) | - |
| Extended | Cancel-Rate Limit | In Standard-Limit enthalten (1000/min) | API Docs |
| Extended | IP-Whitelist | Nicht erforderlich | API Docs |
| Beide | L3 Orderbook | Nicht verfügbar | Confirmed |

### 14.8 Order-to-Trade Ratio & Cancel Restrictions

**Wichtig:** Anders als Binance (die explizite OTR-Limits haben), dokumentieren weder Lighter noch Extended spezifische Cancel-Rate-Restrictions.

**Binance Referenz (zum Vergleich):**
```
Binance Quantitative Rules:
- Unfilled Ratio (UFR) >= 0.99 → Ban
- Invalid Cancellation Ratio (ICR) >= 0.99 → Ban
- Dust Ratio >= 0.9 → Ban
- 10 Bans in 24h → Extended Restriction
```

**Lighter/Extended:**
- Keine dokumentierten OTR-Limits
- ToS verbietet "market manipulation"
- Potenzielle undokumentierte Soft-Limits möglich

### 14.9 Server-Standorte & Latenz

**Keine öffentlichen Informationen zu AWS/GCP Regionen für Lighter oder Extended.**

**Latenz-Optimierung Empfehlungen:**

```python
# Ping-Test für Region-Bestimmung
import subprocess
import statistics

ENDPOINTS = {
    "lighter_api": "api.lighter.xyz",
    "lighter_ws": "mainnet.zklighter.elliot.ai",
    "extended_api": "api.starknet.extended.exchange",
}

def measure_latency(host: str, count: int = 10) -> dict:
    """Misst RTT zu einem Host."""
    result = subprocess.run(
        ["ping", "-c", str(count), host],
        capture_output=True, text=True
    )
    # Parse ping output...
    return {
        "host": host,
        "avg_ms": avg,
        "min_ms": min_val,
        "max_ms": max_val,
    }

# Empfohlene AWS Regionen für Europa:
# - eu-west-1 (Ireland) - Typisch für EU Fintech
# - eu-central-1 (Frankfurt) - Niedrigste Latenz zu DE
```

### 14.10 Bot-Startup Checklist

```python
class ExchangeHealthChecker:
    """
    Startup-Checks vor Bot-Aktivierung.
    """

    async def preflight_checks(self) -> dict:
        results = {
            "timestamp": datetime.utcnow().isoformat(),
            "checks": {},
            "ready": False,
        }

        # 1. API Connectivity
        results["checks"]["lighter_api"] = await self._check_api("lighter")
        results["checks"]["extended_api"] = await self._check_api("extended")

        # 2. WebSocket Connectivity
        results["checks"]["lighter_ws"] = await self._check_ws("lighter")
        results["checks"]["extended_ws"] = await self._check_ws("extended")

        # 3. Market Status
        results["checks"]["lighter_markets"] = await self._check_market_status("lighter")
        results["checks"]["extended_markets"] = await self._check_market_status("extended")

        # 4. Account Balance
        results["checks"]["lighter_balance"] = await self._check_balance("lighter")
        results["checks"]["extended_balance"] = await self._check_balance("extended")

        # 5. Rate Limit Headroom
        results["checks"]["rate_limits"] = await self._check_rate_limits()

        # 6. Sequencer Health (Lighter)
        results["checks"]["lighter_sequencer"] = await self._check_sequencer_health()

        # Aggregate
        results["ready"] = all(
            c.get("ok", False) for c in results["checks"].values()
        )

        return results

    async def _check_market_status(self, exchange: str) -> dict:
        """Prüft ob Markets ACTIVE sind (nicht REDUCE_ONLY)."""
        if exchange == "extended":
            # Check market status field
            markets = await self.extended_client.get_markets()
            inactive = [
                m["name"] for m in markets
                if m["status"] != "ACTIVE"
            ]
            return {
                "ok": len(inactive) == 0,
                "inactive_markets": inactive,
            }
        # ... Lighter implementation
```

---

## Confidence Levels (Aktualisiert)

| Sektion | Confidence | Notizen |
|---------|------------|---------|
| Lighter API | 95% | Direkt aus offizieller Dokumentation |
| Lighter SDK | 95% | GitHub Repository verifiziert |
| Lighter Architecture | 90% | Whitepaper + L2Beat |
| Extended API | 90% | Apiary + GitHub |
| Extended SDK | 95% | GitHub Repository verifiziert (starknet branch) |
| Funding Mechanismen | 95% | Beide Docs bestätigen stündliche Anwendung |
| Smart Contracts | 85% | Indirekte Quellen |
| API Parameter-Typen | 95% | Direkt aus API-Referenz extrahiert |
| STP-Mechanismen | 90% | Lighter Docs + Exchange-Vergleich |
| Liquidation/Insurance | 85% | Docs + Whitepaper |
| Risk-Tier-Tabellen | 90% | Extended Margin Schedule Docs |
| Starknet Config | 95% | GitHub SDK verifiziert |
| L3 Orderbook | 100% | Bestätigt: Nicht verfügbar bei beiden |
| Funding Z-Score | 90% | Implementierung basiert auf Standardmethodik |
| **Sequencer/Matching** | **90%** | Whitepaper + News Reports |
| **WebSocket Config** | **60%** | Meist undokumentiert, Industry Defaults |
| **Maintenance Flags** | **95%** | Extended API dokumentiert |
| **Price Bands** | **95%** | Beide Exchanges dokumentiert |
| **OTR/Cancel Limits** | **40%** | Nicht dokumentiert, nur Binance-Vergleich |
| **Server-Standorte** | **20%** | Keine öffentlichen Informationen |

---

## Zusammenfassung der Recherche

Diese Wissensdatenbank enthält **14 Teile** mit umfassenden technischen Details:

1. **Architektur**: ZK-Rollup (Lighter) vs StarkEx→Starknet (Extended)
2. **API-Grundlagen**: REST + WebSocket Endpunkte
3. **Order-Management**: Alle Order-Typen, Time-in-Force, Reduce-Only
4. **Code-Level Details**: ctypes, Starknet Signing, Error Codes
5. **Historische Daten**: Candles, Funding History mit Pagination
6. **Vollständige API-Referenz**: Alle Endpunkte beider Exchanges
7. **Mark Price & Funding Formeln**: Premium Index, TWAP, Impact Price
8. **API Parameter-Typen**: int64, string, boolean mit Ranges
9. **Self-Trade Prevention**: Lighter (CM-only) vs Extended (konfigurierbar)
10. **Liquidation & Insurance Fund**: LLP, Fees, ADL
11. **Risk-Tier-Tabellen**: 6 Gruppen mit Position Brackets
12. **Infrastructure**: Starknet Config, Asset Resolution, L3 Status
13. **30-Tage Funding-Historie**: Lückenlose Abfrage + Z-Score
14. **Operativer Betrieb & DevOps**: Sequencer, WebSocket, Maintenance, Hidden Limits

**Kritische Erkenntnisse für Arbitrage-Bot:**
- Beide Exchanges: Funding STÜNDLICH (nicht 8h!)
- Lighter: 0% Trading Fees, 1% Liquidation Fee
- Extended: 6 Margin-Gruppen mit unterschiedlichen Limits
- Kein L3 Orderbook bei beiden Exchanges verfügbar
- Z-Score-Berechnung erfordert timestamp-alignment
- **NEU:** Lighter Sequencer kann bei 16x Load ausfallen
- **NEU:** Extended Market-Status-Flag prüfen vor Trading
- **NEU:** Price Bands: ±5% vom Mark Price bei beiden
- **NEU:** WebSocket Keepalive: 20s Ping empfohlen (undokumentiert)

---

*Generiert mit Claude Code Deep Research - 2025-01-17*
*Letzte Aktualisierung: Teil 14 (Operativer Betrieb & DevOps) hinzugefügt*
