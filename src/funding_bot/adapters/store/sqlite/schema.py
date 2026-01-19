"""
SQLite schema and adapters.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal


def _adapt_decimal(value: Decimal) -> str:
    """Convert Decimal to string for SQLite storage."""
    return str(value)


def _convert_decimal(data: bytes) -> Decimal:
    """Convert SQLite string back to Decimal."""
    return Decimal(data.decode("utf-8"))


# Register adapters globally
sqlite3.register_adapter(Decimal, _adapt_decimal)
sqlite3.register_converter("DECIMAL", _convert_decimal)


SCHEMA_VERSION = 2

SCHEMA_SQL = """
-- Trades table
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    execution_state TEXT NOT NULL,

    -- Leg 1
    leg1_exchange TEXT NOT NULL,
    leg1_side TEXT NOT NULL,
    leg1_order_id TEXT,
    leg1_qty DECIMAL DEFAULT '0',
    leg1_filled_qty DECIMAL DEFAULT '0',
    leg1_entry_price DECIMAL DEFAULT '0',
    leg1_exit_price DECIMAL DEFAULT '0',
    leg1_fees DECIMAL DEFAULT '0',

    -- Leg 2
    leg2_exchange TEXT NOT NULL,
    leg2_side TEXT NOT NULL,
    leg2_order_id TEXT,
    leg2_qty DECIMAL DEFAULT '0',
    leg2_filled_qty DECIMAL DEFAULT '0',
    leg2_entry_price DECIMAL DEFAULT '0',
    leg2_exit_price DECIMAL DEFAULT '0',
    leg2_fees DECIMAL DEFAULT '0',

    -- Trade params
    target_qty DECIMAL NOT NULL,
    target_notional_usd DECIMAL NOT NULL,

    -- Funding
    funding_collected DECIMAL DEFAULT '0',
    last_funding_update TEXT,

    -- PnL
    realized_pnl DECIMAL DEFAULT '0',
    unrealized_pnl DECIMAL DEFAULT '0',
    high_water_mark DECIMAL DEFAULT '0',

    -- Metadata
    entry_apy DECIMAL DEFAULT '0',
    entry_spread DECIMAL DEFAULT '0',
    current_apy DECIMAL DEFAULT '0',
    current_lighter_rate DECIMAL DEFAULT '0',
    current_x10_rate DECIMAL DEFAULT '0',
    last_eval_at TEXT,
    close_reason TEXT,

    -- Total fees (leg1 + leg2)
    total_fees DECIMAL DEFAULT '0',

    -- Exit tracking (for performance analytics)
    estimated_exit_pnl DECIMAL DEFAULT '0',
    actual_exit_pnl DECIMAL DEFAULT '0',
    exit_slippage_usd DECIMAL DEFAULT '0',
    exit_spread_pct DECIMAL DEFAULT '0',
    close_attempts INTEGER DEFAULT 0,
    close_duration_seconds DECIMAL DEFAULT '0',

    -- Timestamps
    created_at TEXT NOT NULL,
    opened_at TEXT,
    closed_at TEXT,

    -- Event log (JSON)
    events TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);

-- Events table (append-only audit log)
CREATE TABLE IF NOT EXISTS trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    trade_id TEXT,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE INDEX IF NOT EXISTS idx_events_trade ON trade_events(trade_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON trade_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON trade_events(timestamp);

-- Funding events
CREATE TABLE IF NOT EXISTS funding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    exchange TEXT NOT NULL,
    amount DECIMAL NOT NULL,
    timestamp TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE INDEX IF NOT EXISTS idx_funding_trade ON funding_events(trade_id);

-- PnL snapshots
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    realized_pnl DECIMAL NOT NULL,
    unrealized_pnl DECIMAL NOT NULL,
    funding DECIMAL NOT NULL,
    fees DECIMAL NOT NULL,
    timestamp TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE INDEX IF NOT EXISTS idx_pnl_trade ON pnl_snapshots(trade_id);

-- Funding rate history (for analytics and APY stability detection)
CREATE TABLE IF NOT EXISTS funding_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    rate_hourly DECIMAL NOT NULL,
    rate_apy DECIMAL,
    next_funding_time TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funding_history_symbol ON funding_history(symbol);
CREATE INDEX IF NOT EXISTS idx_funding_history_timestamp ON funding_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_funding_history_exchange ON funding_history(exchange);
CREATE UNIQUE INDEX IF NOT EXISTS idx_funding_history_unique ON funding_history(symbol, exchange, timestamp);

-- Execution attempts (decision log + KPIs)
CREATE TABLE IF NOT EXISTS execution_attempts (
    attempt_id TEXT PRIMARY KEY,
    trade_id TEXT,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT,

    -- Common KPIs (optional)
    apy DECIMAL,
    expected_value_usd DECIMAL,
    breakeven_hours DECIMAL,
    opp_spread_pct DECIMAL,
    entry_spread_pct DECIMAL,
    max_spread_pct DECIMAL,
    realized_spread_post_leg1_pct DECIMAL,
    leg1_fill_seconds DECIMAL,
    leg2_fill_seconds DECIMAL,
    leg1_slippage_bps DECIMAL,
    leg2_slippage_bps DECIMAL,
    total_fees DECIMAL,

    -- Hedge latency KPIs (milliseconds)
    leg1_to_leg2_submit_ms DECIMAL,
    leg1_to_leg2_ack_ms DECIMAL,
    leg2_place_ack_ms DECIMAL,

    -- Flexible payload
    data_json TEXT NOT NULL,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_symbol ON execution_attempts(symbol);
CREATE INDEX IF NOT EXISTS idx_attempts_trade ON execution_attempts(trade_id);
CREATE INDEX IF NOT EXISTS idx_attempts_created ON execution_attempts(created_at);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON execution_attempts(status);

-- Schema version
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version (version) VALUES (2);

-- Surge Pro single-leg trades
CREATE TABLE IF NOT EXISTS surge_trades (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    side TEXT NOT NULL,
    qty DECIMAL NOT NULL,
    entry_price DECIMAL DEFAULT '0',
    exit_price DECIMAL DEFAULT '0',
    fees DECIMAL DEFAULT '0',
    status TEXT NOT NULL,
    entry_order_id TEXT,
    entry_client_order_id TEXT,
    exit_order_id TEXT,
    exit_client_order_id TEXT,
    entry_reason TEXT,
    exit_reason TEXT,
    created_at TEXT NOT NULL,
    opened_at TEXT,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_surge_trades_symbol ON surge_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_surge_trades_status ON surge_trades(status);
CREATE INDEX IF NOT EXISTS idx_surge_trades_opened_at ON surge_trades(opened_at);
CREATE INDEX IF NOT EXISTS idx_surge_trades_closed_at ON surge_trades(closed_at);

-- ============================================================================
-- HISTORICAL ANALYSIS TABLES (v2)
-- ============================================================================

-- Minute-level candle data for crash detection
CREATE TABLE IF NOT EXISTS funding_candles_minute (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    mark_price DECIMAL,
    index_price DECIMAL,
    spread_bps DECIMAL,
    funding_rate_hourly DECIMAL NOT NULL,
    funding_apy DECIMAL,
    fetched_at TEXT NOT NULL,
    data_source TEXT,

    UNIQUE(symbol, exchange, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_candles_sym_ts ON funding_candles_minute(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_candles_exch_ts ON funding_candles_minute(exchange, timestamp);
CREATE INDEX IF NOT EXISTS idx_candles_timestamp ON funding_candles_minute(timestamp);

-- Crash events for recovery time analysis
CREATE TABLE IF NOT EXISTS funding_crash_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    crash_start_time TEXT NOT NULL,
    crash_end_time TEXT,
    crash_duration_minutes INTEGER,
    pre_crash_apy DECIMAL NOT NULL,
    crash_min_apy DECIMAL NOT NULL,
    crash_depth_pct DECIMAL NOT NULL,
    recovery_time_minutes INTEGER,
    recovery_threshold_pct DECIMAL DEFAULT 80,
    detected_at TEXT NOT NULL,
    detection_method TEXT
);

CREATE INDEX IF NOT EXISTS idx_crashes_sym_time ON funding_crash_events(symbol, crash_start_time);
CREATE INDEX IF NOT EXISTS idx_crashes_ongoing ON funding_crash_events(symbol, crash_end_time)
    WHERE crash_end_time IS NULL;

-- Volatility metrics per symbol
CREATE TABLE IF NOT EXISTS funding_volatility_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    period_days INTEGER NOT NULL,
    calculated_at TEXT NOT NULL,

    -- Volatility metrics
    hourly_std_dev DECIMAL,
    hourly_range_avg DECIMAL,
    crash_frequency INTEGER,
    avg_crash_duration_minutes DECIMAL,
    avg_recovery_time_minutes DECIMAL,

    -- Quantiles for prediction
    p25_apy DECIMAL,
    p50_apy DECIMAL,
    p75_apy DECIMAL,
    p90_apy DECIMAL,
    p95_apy DECIMAL,

    UNIQUE(symbol, period_days, calculated_at)
);
"""
