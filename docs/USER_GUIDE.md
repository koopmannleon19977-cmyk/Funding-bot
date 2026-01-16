# User Guide - Funding Arbitrage Bot

> **Version**: 2.0.0
> **Last Updated**: 2026-01-16

---

## Table of Contents

1. [Introduction](#introduction)
2. [Quick Start](#quick-start)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running the Bot](#running-the-bot)
6. [Understanding the Dashboard](#understanding-the-dashboard)
7. [Trading Strategies](#trading-strategies)
8. [Exit Rules](#exit-rules)
9. [Risk Management](#risk-management)
10. [Monitoring & Alerts](#monitoring--alerts)
11. [Troubleshooting](#troubleshooting)
12. [FAQ](#faq)

---

## Introduction

The Funding Arbitrage Bot is a production-grade trading system that exploits funding rate differentials between perpetual futures exchanges. It maintains **delta-neutral positions** by going long on one exchange and short on another, collecting the net funding rate difference as profit.

### Supported Exchanges

| Exchange | Type | Role | Rate Limit |
|----------|------|------|------------|
| **Lighter** | DEX | Primary (Maker) | 24,000 req/min (Premium) |
| **X10 Extended** | CEX | Secondary (Hedge) | Standard |

### How It Works

```
1. SCAN    → Find funding rate opportunities across symbols
2. FILTER  → Apply APY, spread, and liquidity filters
3. EXECUTE → Open delta-neutral position (Long + Short)
4. HOLD    → Collect funding payments hourly
5. EXIT    → Close when conditions change
```

---

## Quick Start

```bash
# 1. Clone and install
git clone <repo-url>
cd funding-bot
python -m venv venv
venv\Scripts\activate  # Windows
pip install -e ".[dev]"

# 2. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 3. Run in paper mode (default)
python -m funding_bot

# 4. Enable live trading (when ready)
# Set live_trading: true in config/config.yaml
```

---

## Installation

### Requirements

- **Python**: 3.14+
- **OS**: Windows, Linux, macOS
- **Memory**: 512MB+ RAM recommended
- **Network**: Stable internet connection

### Step-by-Step Installation

```bash
# 1. Create virtual environment
python -m venv venv

# 2. Activate virtual environment
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 3. Upgrade pip
python -m pip install -U pip

# 4. Install with development dependencies
pip install -e ".[dev]"
```

### Exchange SDK Installation (Optional)

The bot works without exchange SDKs for paper trading. For live trading:

```bash
# Install exchange SDKs (optional, for live trading)
pip install -r requirements-exchange.txt
```

### Verify Installation

```bash
# Run unit tests (should pass without SDKs)
pytest tests/unit/ -q

# Check version
python -c "import funding_bot; print(funding_bot.__version__)"
```

---

## Configuration

### Environment Variables (.env)

Create a `.env` file in the project root:

```bash
# Lighter Exchange (DEX)
LIGHTER_API_KEY=your_lighter_api_key
LIGHTER_PRIVATE_KEY=0x_your_lighter_private_key

# X10 Exchange (CEX)
X10_API_KEY=your_x10_api_key
X10_PRIVATE_KEY=0x_your_x10_private_key

# Telegram Notifications (Optional)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

### Main Configuration (config/config.yaml)

#### Trading Mode

```yaml
# IMPORTANT: Start with paper mode!
live_trading: false    # false = paper mode (no real orders)
testing_mode: false    # true = extra debug logging
```

#### Position Sizing

```yaml
trading:
  desired_notional_usd: "350.0"    # Position size per trade
  max_open_trades: 1               # Max concurrent positions
  leverage_multiplier: "7.0"       # Target leverage (6-8 recommended)
```

#### Entry Filters

```yaml
trading:
  min_apy_filter: "0.35"              # Minimum 35% APY required
  min_expected_profit_entry_usd: "0.80"  # Min expected profit
  max_spread_filter_percent: "0.0016" # Max 0.16% spread
  max_breakeven_hours: "24.0"         # Max 24h to breakeven
```

#### Hold Duration

```yaml
trading:
  min_hold_seconds: 172800   # 48 hours minimum hold
  max_hold_hours: "240.0"    # 10 days maximum hold
```

The minimum hold time is **critical** for profitability:
- **48 hours** = ~15 trades/month (recommended)
- **24 hours** = ~30 trades/month (more fees)

#### Exit Rules

```yaml
trading:
  # Economic exits
  exit_ev_enabled: true              # Net EV exit
  exit_ev_horizon_hours: "12.0"      # Look-ahead window

  # Early take profit
  early_take_profit_enabled: true
  early_take_profit_net_usd: "4.00"  # Exit if $4+ profit before min_hold

  # Funding velocity (detects APY crashes)
  funding_velocity_exit_enabled: true
  velocity_threshold_hourly: "-0.0015"
```

---

## Running the Bot

### Paper Mode (Recommended First)

```bash
# Default: paper mode (live_trading: false)
python -m funding_bot
```

In paper mode, the bot:
- Scans for opportunities
- Calculates optimal entries/exits
- Logs all decisions
- Does NOT place real orders

### Live Trading Mode

```yaml
# config/config.yaml
live_trading: true
```

```bash
python -m funding_bot
```

### Background Execution

```bash
# Linux/macOS
nohup python -m funding_bot > bot.log 2>&1 &

# Windows (PowerShell)
Start-Process -NoNewWindow python -ArgumentList "-m funding_bot"

# Using screen (Linux)
screen -S funding-bot
python -m funding_bot
# Ctrl+A, D to detach
```

### Docker (Optional)

```bash
# Build
docker build -t funding-bot .

# Run
docker run -d --name funding-bot \
  -v $(pwd)/.env:/app/.env \
  -v $(pwd)/data:/app/data \
  funding-bot
```

---

## Understanding the Dashboard

The bot displays a real-time terminal dashboard:

```
╔══════════════════════════════════════════════════════════════════════════╗
║                     FUNDING ARBITRAGE BOT v2.0.0                         ║
╠══════════════════════════════════════════════════════════════════════════╣
║ Mode: LIVE          Account: Premium        Uptime: 4h 23m               ║
╠══════════════════════════════════════════════════════════════════════════╣
║ ACTIVE POSITIONS                                                          ║
╠──────────┬──────────┬──────────┬──────────┬──────────┬──────────────────╣
║ Symbol   │ APY      │ Notional │ PnL      │ Age      │ Status           ║
╠──────────┼──────────┼──────────┼──────────┼──────────┼──────────────────╣
║ ETH      │  45.2%   │ $350.00  │ +$2.45   │ 12.3h    │ OPEN             ║
║ BTC      │  32.8%   │ $350.00  │ +$1.12   │  6.5h    │ OPEN             ║
╠══════════════════════════════════════════════════════════════════════════╣
║ OPPORTUNITIES                                                             ║
╠──────────┬──────────┬──────────┬──────────┬──────────────────────────────╣
║ Symbol   │ APY      │ Spread   │ EV/hour  │ Status                       ║
╠──────────┼──────────┼──────────┼──────────┼──────────────────────────────╣
║ SOL      │  68.4%   │  0.08%   │ $0.12    │ READY                        ║
║ AVAX     │  41.2%   │  0.15%   │ $0.08    │ SPREAD_HIGH                  ║
╠══════════════════════════════════════════════════════════════════════════╣
║ STATISTICS                                                                ║
║ Total PnL: +$156.78   │   Trades: 45   │   Win Rate: 82%                ║
╚══════════════════════════════════════════════════════════════════════════╝
```

### Status Indicators

| Status | Meaning |
|--------|---------|
| `OPEN` | Position active, collecting funding |
| `CLOSING` | Exit triggered, closing in progress |
| `READY` | Opportunity meets all criteria |
| `APY_LOW` | Below minimum APY threshold |
| `SPREAD_HIGH` | Spread exceeds maximum |
| `COOLDOWN` | Recently exited, waiting |

---

## Trading Strategies

### Entry Strategy

The bot uses a **quality-over-quantity** approach:

1. **APY Filter**: Only trades with 35%+ APY
2. **Spread Filter**: Maximum 0.16% bid-ask spread
3. **Liquidity Gate**: Sufficient depth on both sides
4. **EV Check**: Positive expected value after fees
5. **Breakeven Time**: Must recover costs within 24 hours

### Position Direction

```
If Lighter Rate > X10 Rate:
  → Long X10, Short Lighter
  → Receive funding from Lighter shorts

If X10 Rate > Lighter Rate:
  → Long Lighter, Short X10
  → Receive funding from X10 shorts
```

### Execution Flow

```
1. Leg1 (Lighter - Maker)
   └─ Post limit order at competitive price
   └─ Wait for fill (max 30s)
   └─ Escalate to taker if needed

2. Leg2 (X10 - Hedge)
   └─ Immediately hedge after Leg1 fill
   └─ IOC order with slippage tolerance
   └─ Retry up to 3 times

3. Position Open
   └─ Both legs filled
   └─ Start collecting funding
```

---

## Exit Rules

### Priority Layers

Exits are evaluated in priority order (first match wins):

#### Layer 1: Emergency (Override min_hold)

| Rule | Trigger | Action |
|------|---------|--------|
| **Broken Hedge** | One leg missing | Immediate close |
| **Liquidation** | < 15% distance | Immediate close |
| **Delta Drift** | > 3% imbalance | Rebalance or close |

#### Layer 2: Economic (After min_hold)

| Rule | Trigger | Action |
|------|---------|--------|
| **Net EV Negative** | EV horizon < costs | Close position |
| **Funding Flip** | Direction reversed | Close position |
| **Opportunity Cost** | Better trade available | Rotate |

#### Layer 3: Profit Taking

| Rule | Trigger | Action |
|------|---------|--------|
| **Early TP** | $4+ profit (any time) | Close position |
| **Velocity Exit** | APY declining fast | Close position |
| **Z-Score Exit** | 2-sigma deviation | Close position |

### Configuring Exit Rules

```yaml
trading:
  # Emergency
  delta_bound_max_delta_pct: "0.03"     # 3% max drift
  liquidation_distance_min_pct: "15.0"  # 15% buffer

  # Economic
  exit_ev_enabled: true
  exit_ev_horizon_hours: "12.0"
  funding_flip_hours_threshold: "0.0"   # Immediate on flip

  # Profit Taking
  early_take_profit_net_usd: "4.00"
  funding_velocity_exit_enabled: true
```

---

## Risk Management

### Position Limits

```yaml
risk:
  max_consecutive_failures: 3      # Circuit breaker
  max_drawdown_pct: "0.20"         # 20% max drawdown
  max_exposure_pct: "80.0"         # 80% max capital exposure
  max_trade_size_pct: "50.0"       # 50% max per trade
  min_free_margin_pct: "0.05"      # 5% margin reserve
```

### Circuit Breaker

After 3 consecutive execution failures:
- Bot pauses new entries
- Monitors existing positions
- Requires manual restart

### Delta Protection

The bot continuously monitors position delta:

| Drift | Action |
|-------|--------|
| 0-1% | Normal |
| 1-3% | Warning logged |
| > 3% | Rebalance triggered |

Rebalancing restores delta neutrality by adjusting the smaller leg.

---

## Monitoring & Alerts

### Telegram Notifications

Enable Telegram for real-time alerts:

```yaml
telegram:
  enabled: true
```

```bash
# .env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Notifications sent for:
- Trade opened/closed
- Exit triggered (with reason)
- Errors and warnings
- Daily summary

### Log Files

```
logs/
├── funding_bot_YYYYMMDD_HHMMSS.log  # Human-readable
└── funding_bot_json.jsonl           # JSON format (for analysis)
```

### Log Levels

```yaml
logging:
  level: "INFO"      # INFO, DEBUG, WARNING, ERROR
  json_enabled: true
```

### Key Log Tags

| Tag | Meaning |
|-----|---------|
| `[TRADE]` | Trade events (open/close) |
| `[PROFIT]` | PnL updates |
| `[HEALTH]` | Health checks |
| `[SCAN]` | Opportunity scans |

---

## Troubleshooting

### Common Issues

#### "WebSocket disconnected"

**Cause**: Network instability or exchange maintenance

**Solution**: Bot auto-reconnects with circuit breaker
```yaml
websocket:
  circuit_breaker_threshold: 10
  circuit_breaker_cooldown_seconds: "60.0"
```

#### "Too Many Websocket Messages"

**Cause**: Subscribed to too many Lighter streams

**Solution**: Limit symbols
```yaml
websocket:
  market_data_streams_symbols: ["ETH", "BTC", "SOL"]
  market_data_streams_max_symbols: 6
```

#### "Insufficient Balance"

**Cause**: Not enough margin on exchange

**Solution**:
1. Check balances on both exchanges
2. Reduce `desired_notional_usd`
3. Ensure `min_free_margin_pct` is respected

#### "Order Rejected"

**Cause**: Various exchange-specific reasons

**Solution**: Check logs for rejection reason
```bash
grep "rejected" logs/funding_bot_json.jsonl
```

#### "Ghost Position Detected"

**Cause**: Position exists on exchange but not in database

**Solution**:
```yaml
reconciliation:
  auto_import_ghosts: true  # Import into DB
  # OR
  auto_close_ghosts: true   # Close on exchange
```

### Debug Mode

For detailed troubleshooting:

```yaml
testing_mode: true  # Extra diagnostics
logging:
  level: "DEBUG"
```

### Database Issues

```bash
# Check database integrity
sqlite3 data/funding_v2.db "PRAGMA integrity_check;"

# Backup database
cp data/funding_v2.db data/funding_v2.db.backup
```

---

## FAQ

### General

**Q: Is paper mode safe to run?**

A: Yes, paper mode never places real orders. It's purely simulation.

**Q: How much capital do I need?**

A: Minimum $300-500 recommended for meaningful returns. The default config is optimized for ~$400.

**Q: What's a realistic return?**

A: With optimized settings, expect 5-15% monthly during favorable funding conditions. Returns vary with market conditions.

### Trading

**Q: Why isn't the bot opening trades?**

A: Check these filters:
- `min_apy_filter` (default 35%)
- `max_spread_filter_percent` (default 0.16%)
- `min_l1_notional_usd` (liquidity check)

Lower thresholds if needed, but understand the tradeoffs.

**Q: Why did a trade exit early?**

A: Check the `close_reason` in logs. Common reasons:
- `NET_EV_NEGATIVE` - Expected value turned negative
- `FUNDING_FLIP` - Funding direction reversed
- `EARLY_TP` - Early take profit triggered

**Q: Can I trade multiple symbols?**

A: Yes, set `max_open_trades` > 1. But single-trade mode often outperforms due to quality filtering.

### Technical

**Q: Do I need exchange SDKs?**

A: No for paper mode. Yes for live trading (install via `requirements-exchange.txt`).

**Q: How do I update the bot?**

A:
```bash
git pull
pip install -e ".[dev]"
```

**Q: Where is trade data stored?**

A: SQLite database at `data/funding_v2.db`

### Exchanges

**Q: What's the difference between Standard and Premium Lighter?**

A: Premium accounts have 400x higher rate limits (24,000 vs 60 req/min), enabling faster market data and execution.

**Q: Can I use other exchanges?**

A: Currently only Lighter and X10 are supported. The architecture supports adding new exchanges via the `ExchangePort` interface.

---

## Getting Help

- **Documentation**: See [TECHNICAL.md](TECHNICAL.md) for detailed technical docs
- **Issues**: Report bugs at the project repository
- **Logs**: Include relevant logs when reporting issues

---

## Appendix: Configuration Reference

### All Configuration Keys

See [config/config.yaml](../src/funding_bot/config/config.yaml) for the complete configuration with comments.

### Key Defaults

| Setting | Default | Description |
|---------|---------|-------------|
| `live_trading` | `false` | Paper mode by default |
| `desired_notional_usd` | `350.0` | Position size |
| `min_apy_filter` | `0.35` | 35% minimum APY |
| `min_hold_seconds` | `172800` | 48 hours |
| `max_spread_filter_percent` | `0.0016` | 0.16% max spread |
| `early_take_profit_net_usd` | `4.00` | $4 early TP |

---

**Good luck with your trading!**
