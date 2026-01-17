# User Guide

## Introduction

This guide walks you through setting up and operating the Funding Bot for delta-neutral funding rate arbitrage between Lighter and X10 exchanges.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Running the Bot](#running-the-bot)
5. [Monitoring](#monitoring)
6. [Common Operations](#common-operations)
7. [Troubleshooting](#troubleshooting)
8. [FAQ](#faq)

---

## Prerequisites

### System Requirements

- **Operating System**: Windows 10/11, Linux, or macOS
- **Python**: 3.11 or higher
- **Memory**: Minimum 2GB RAM
- **Storage**: 1GB free disk space
- **Network**: Stable internet connection

### Exchange Accounts

Before starting, you need active accounts on both exchanges:

#### Lighter Exchange
1. Create account at [lighter.xyz](https://lighter.xyz)
2. Generate API credentials in account settings
3. Note your account index (usually 0)

#### X10 Exchange
1. Create account at [extended.exchange](https://extended.exchange)
2. Generate API key and secret
3. Generate Stark private key for signing

### Capital Requirements

| Parameter | Minimum | Recommended |
|-----------|---------|-------------|
| Starting Capital | $200 | $400+ |
| Per Exchange | $100 | $200+ |
| Position Size | $50 | $350 |

---

## Installation

### Step 1: Clone Repository

```bash
git clone https://github.com/your-org/funding-bot.git
cd funding-bot
```

### Step 2: Create Virtual Environment

**Windows:**
```bash
python -m venv venv
.\venv\Scripts\activate
```

**Linux/macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Dependencies

```bash
# Core dependencies
pip install -e .

# Exchange SDKs
pip install x10-python-trading-starknet>=0.0.17

# Lighter SDK (from GitHub)
pip install git+https://github.com/elliottech/lighter-python.git
```

### Step 4: Verify Installation

```bash
# Check Python version
python --version  # Should be 3.11+

# Test import
python -c "from funding_bot import __version__; print(__version__)"
```

---

## Configuration

### Environment Variables

Create a `.env` file in the project root:

```env
# ============================================================
# LIGHTER EXCHANGE
# ============================================================
LIGHTER_PRIVATE_KEY=0x1234567890abcdef...  # Your Ethereum private key
LIGHTER_ACCOUNT_INDEX=0                     # Usually 0 for primary account

# ============================================================
# X10 EXCHANGE
# ============================================================
X10_API_KEY=your_api_key_here
X10_API_SECRET=your_api_secret_here
X10_STARK_PRIVATE_KEY=0xabcdef1234567890...  # Stark curve private key

# ============================================================
# TELEGRAM NOTIFICATIONS (Optional)
# ============================================================
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=-1001234567890
```

### Main Configuration (config.yaml)

The main configuration file is located at `src/funding_bot/config/config.yaml`.

#### Trading Mode

```yaml
# Set to false for paper trading (no real orders)
live_trading: true

# Enable for extra diagnostics
testing_mode: false
```

#### Position Sizing

```yaml
trading:
  # Position size in USD
  desired_notional_usd: "350.0"

  # Maximum concurrent positions
  max_open_trades: 1

  # Leverage multiplier (6-8 recommended)
  leverage_multiplier: "7.0"
```

#### Entry Filters

```yaml
trading:
  # Minimum APY to enter (35% = 0.35)
  min_apy_filter: "0.35"

  # Minimum expected profit per trade
  min_expected_profit_entry_usd: "0.80"

  # Maximum spread at entry
  max_spread_filter_percent: "0.0016"  # 0.16%
```

#### Hold Time Settings

```yaml
trading:
  # Minimum hold time (48 hours = 172800 seconds)
  min_hold_seconds: 172800

  # Maximum hold time
  max_hold_hours: "240.0"  # 10 days
```

#### Exit Settings

```yaml
trading:
  # Minimum profit to exit
  min_profit_exit_usd: "3.00"

  # Early take-profit threshold
  early_take_profit_enabled: true
  early_take_profit_net_usd: "4.00"

  # Funding flip (exit when funding turns negative)
  funding_flip_hours_threshold: "0.0"  # Immediate exit
```

---

## Running the Bot

### Standard Start

```bash
# Activate virtual environment
.\venv\Scripts\activate  # Windows
source venv/bin/activate  # Linux/macOS

# Run the bot
python main.py
```

### Using Batch Files (Windows)

```bash
# Standard start
start.bat

# Debug mode with extra logging
start-live-debug.bat
```

### CLI Commands

```bash
# Standard run
funding-bot

# Doctor mode (diagnostics)
python -m funding_bot doctor

# Reconcile positions
python -m funding_bot reconcile

# Close all positions
python -m funding_bot close-all

# Backfill historical data
python -m funding_bot backfill
```

### Paper Trading Mode

To test without real money, set in `config.yaml`:

```yaml
live_trading: false
```

The bot will scan for opportunities and log decisions without placing orders.

---

## Monitoring

### Console Output

When running, the bot displays:

```
2026-01-17 14:30:00 | INFO | Starting Funding Bot v2.0.0
2026-01-17 14:30:01 | INFO | Lighter adapter initialized
2026-01-17 14:30:02 | INFO | X10 adapter initialized
2026-01-17 14:30:03 | INFO | Scanning for opportunities...
2026-01-17 14:30:05 | INFO | [ETH] APY: 45.2% | Spread: 0.08% | CANDIDATE
2026-01-17 14:30:10 | INFO | Opening trade: ETH | $350 | APY: 45.2%
```

### Log Files

Logs are stored in the `logs/` directory:

| File | Content |
|------|---------|
| `funding_bot_YYYYMMDD_HHMMSS.log` | Full session log |
| `funding_bot_json.jsonl` | Structured JSON logs |

### Telegram Notifications

If configured, you'll receive:

- **Trade Opened**: Symbol, APY, position size
- **Trade Closed**: Symbol, PnL, reason
- **Errors**: Critical failures and warnings

### Database

Trade history is stored in `data/funding_v2.db`. You can query it with SQLite:

```bash
sqlite3 data/funding_v2.db

# View recent trades
SELECT * FROM trades ORDER BY created_at DESC LIMIT 10;

# Check active positions
SELECT * FROM trades WHERE status = 'OPEN';
```

---

## Common Operations

### Starting Fresh

To reset and start fresh:

```bash
# Backup existing data
mv data/funding_v2.db data/funding_v2.db.backup

# Clear logs
rm -rf logs/*

# Start bot (new database will be created)
python main.py
```

### Closing All Positions

To manually close all positions:

```bash
python -m funding_bot close-all
```

Or set in `config.yaml` before stopping:

```yaml
shutdown:
  close_positions_on_exit: true
```

### Reconciling Positions

If positions get out of sync:

```bash
python -m funding_bot reconcile
```

This will:
1. Fetch positions from both exchanges
2. Compare with database records
3. Optionally import "ghost" positions

### Adjusting Settings While Running

Most settings require a restart. However, you can:

1. Stop the bot (Ctrl+C)
2. Edit `config.yaml`
3. Restart the bot

The bot will pick up new settings on startup.

---

## Troubleshooting

### Connection Issues

**Problem**: "Failed to connect to exchange"

**Solutions**:
1. Check internet connection
2. Verify API credentials in `.env`
3. Check exchange status pages
4. Try increasing WebSocket timeouts:

```yaml
websocket:
  ping_timeout: "15.0"
  reconnect_delay_initial: "5.0"
```

### Position Mismatch

**Problem**: "Ghost position detected"

**Solutions**:
1. Run reconcile command: `python -m funding_bot reconcile`
2. Enable auto-import in config:

```yaml
reconciliation:
  auto_import_ghosts: true
```

3. Or manually close on exchange web interface

### Order Failures

**Problem**: "Order rejected" or "Insufficient margin"

**Solutions**:
1. Check available balance on exchanges
2. Reduce position size:

```yaml
trading:
  desired_notional_usd: "200.0"  # Reduce from 350
```

3. Check if symbol is tradeable
4. Verify leverage settings

### High Latency

**Problem**: Slow order fills or stale data

**Solutions**:
1. Check network latency to exchange servers
2. Enable WebSocket streams for hot symbols:

```yaml
websocket:
  market_data_streams_enabled: true
  market_data_streams_symbols: ["ETH", "BTC"]
```

3. Reduce scan interval:

```yaml
trading:
  opportunity_scan_interval_seconds: "3.0"
```

### Circuit Breaker Triggered

**Problem**: "Circuit breaker OPEN"

**Solutions**:
1. Wait for cooldown (default 5 minutes)
2. Check logs for failure reasons
3. Reduce max_consecutive_failures if too sensitive:

```yaml
risk:
  max_consecutive_failures: 5  # Increase from 3
```

---

## FAQ

### How much capital do I need?

Minimum $200 total ($100 per exchange). Recommended $400+ for better opportunities.

### What's the expected return?

Targeting 10% monthly (~120% APY). Actual returns depend on market conditions and funding rate opportunities.

### How long should I hold positions?

Default minimum hold is 48 hours. This reduces execution costs and improves net profitability.

### Can I run multiple instances?

Not recommended. Each instance would compete for the same opportunities and could cause position conflicts.

### Is it safe to stop the bot?

Yes. By default, positions remain open when you stop. Set `close_positions_on_exit: true` to close all positions on shutdown.

### How do I change the symbols being traded?

The bot automatically trades all supported symbols. To blacklist symbols:

```yaml
trading:
  blacklist_symbols: ["POPCAT", "PENGU"]
```

### What happens during exchange maintenance?

The bot detects maintenance and pauses trading. It will resume automatically when exchanges are back online.

### How do I update to a new version?

```bash
# Pull latest changes
git pull origin main

# Update dependencies
pip install -e . --upgrade

# Restart bot
python main.py
```

### How do I backup my data?

```bash
# Copy database and logs
cp data/funding_v2.db backups/funding_v2_$(date +%Y%m%d).db
cp -r logs backups/logs_$(date +%Y%m%d)
```

---

## Support

For issues and questions:

1. Check the [GitHub Issues](https://github.com/your-org/funding-bot/issues)
2. Review the [Technical Documentation](TECHNICAL.md)
3. Search existing issues before creating new ones

When reporting issues, please include:
- Bot version
- Error messages from logs
- Configuration (without sensitive data)
- Steps to reproduce
