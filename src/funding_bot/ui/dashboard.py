"""
Realtime TUI dashboard for funding-bot.

Run via `DASHBOARD.bat` (Windows) or:
  set PYTHONPATH=%CD%\\src
  python -m funding_bot.ui.dashboard --refresh 15

Data sources:
- SQLite DB (`data/funding_v2.db`) for trades + execution_attempts
- Latest log file (`logs/funding_bot_*.log`) for HEARTBEAT/HEALTH + tail highlights
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from funding_bot.utils.decimals import safe_decimal as _safe_decimal

console = Console()

_MIGRATION_LAST_ATTEMPT: datetime | None = None
_MIGRATION_DONE = False


_HEALTH_RE = re.compile(r"\[HEALTH\]\s*(?P<kv>.+)$")
_KV_RE = re.compile(r"(?P<k>[a-zA-Z_]+)=(?P<v>[^,]+)")
_SIZING_RE = re.compile(
    r"Sizing Check: Equity=\$(?P<equity>[\d\.]+)\s+"
    r"RiskCap=\$(?P<riskcap>[\d\.]+)\s+Used=\$(?P<used>[\d\.]+)\s+->\s+"
    r"RemRisk=\$(?P<remrisk>[\d\.]+)\.\s+"
    r"LiquidityCap=\$(?P<liqcap>[\d\.]+)\s+"
    r"\(L-85%:\$(?P<lcap>[\d\.]+)/X-95%:\$(?P<xcap>[\d\.]+)\)\s+->\s+"
    r"FINAL AVAILABLE=\$(?P<avail>[\d\.]+)"
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_iso(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        # Stored as ISO strings by the SQLite store.
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _truncate(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


def _latest_log_path(log_dir: Path) -> Path | None:
    if not log_dir.exists():
        return None
    files = sorted(log_dir.glob("funding_bot_*.log"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _tail_lines(path: Path, *, max_bytes: int, max_lines: int) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes), 0)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-max_lines:]
    except Exception:
        return []


def _parse_health_from_lines(lines: Iterable[str]) -> dict[str, str]:
    # newest first
    for raw in reversed(list(lines)):
        m = _HEALTH_RE.search(raw)
        if not m:
            continue
        kv = m.group("kv")
        out: dict[str, str] = {}
        for km in _KV_RE.finditer(kv):
            out[km.group("k")] = km.group("v").strip()
        return out
    return {}


def _find_last_matching(lines: Iterable[str], needles: tuple[str, ...]) -> str | None:
    for raw in reversed(list(lines)):
        if any(n in raw for n in needles):
            return raw.strip()
    return None


def _highlight_log(lines: list[str], *, max_items: int = 8) -> list[str]:
    needles = (
        "[CRITICAL]",
        "[ERROR]",
        "BROKEN HEDGE",
        "Trading paused:",
        "Rollback FAILED",
        "Positions still open",
        "Zombie detected",
        "Ghost detected",
        "Side mismatch",
        "Close verification failed",
    )
    picked: list[str] = []
    for raw in reversed(lines):
        if any(n in raw for n in needles):
            picked.append(raw.strip())
        if len(picked) >= max_items:
            break
    return list(reversed(picked))


def _parse_percent(text: str) -> Decimal | None:
    if not text:
        return None
    s = text.strip().replace("%", "")
    try:
        return Decimal(s) / (Decimal("100") if "%" in text else Decimal("1"))
    except Exception:
        return None


def _parse_float(text: str) -> float | None:
    if not text:
        return None
    try:
        return float(str(text).strip())
    except Exception:
        return None


def _load_thresholds(config_path: Path) -> dict[str, Decimal | int]:
    """
    Load a few risk/trading thresholds from config.yaml (best-effort).
    """
    defaults: dict[str, Decimal | int] = {
        "max_open_trades": 4,
        "max_exposure_pct": Decimal("80.0"),
        "min_free_margin_pct": Decimal("0.05"),
        "max_drawdown_pct": Decimal("0.20"),
        "leverage_multiplier": Decimal("3.0"),
        "x10_leverage_multiplier": Decimal("10.0"),
    }
    try:
        import yaml  # type: ignore
    except Exception:
        return defaults

    if not config_path.exists():
        return defaults

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8", errors="replace")) or {}
        trading = data.get("trading") or {}
        risk = data.get("risk") or {}
        out = dict(defaults)
        if "max_open_trades" in trading:
            out["max_open_trades"] = int(trading["max_open_trades"])
        if "leverage_multiplier" in trading:
            out["leverage_multiplier"] = Decimal(str(trading["leverage_multiplier"]))
        if "x10_leverage_multiplier" in trading:
            out["x10_leverage_multiplier"] = Decimal(str(trading["x10_leverage_multiplier"]))
        if "max_exposure_pct" in risk:
            out["max_exposure_pct"] = Decimal(str(risk["max_exposure_pct"]))
        if "min_free_margin_pct" in risk:
            out["min_free_margin_pct"] = Decimal(str(risk["min_free_margin_pct"]))
        if "max_drawdown_pct" in risk:
            out["max_drawdown_pct"] = Decimal(str(risk["max_drawdown_pct"]))
        return out
    except Exception:
        return defaults


def _infer_last_decision(tail: list[str]) -> tuple[str, str]:
    """
    Infer 'why no trade started' from recent log lines.
    Returns (title, detail).
    """
    if not tail:
        return "No logs", "No log tail available."

    needles: list[tuple[str, str, str]] = [
        ("Executing", "Executing opportunity", "Executing opportunity:"),
        ("Paused", "Trading paused", "Trading paused:"),
        ("Paused", "Trading paused", "Skipping execution (trading paused):"),
        ("Max trades", "At max trades", "At max trades"),
        ("Cooldown", "Failure cooldown", "Adding "),
        ("Rejected", "Rejected after validation", "rejected after fresh data fetch"),
        ("Rejected", "Rejecting candidate", "Rejecting "),
        ("No opps", "No opportunities", "Found 0 opportunities"),
    ]

    for raw in reversed(tail):
        line = raw.strip()
        for _cat, title, token in needles:
            if token in line:
                return title, _truncate(line, 220)

    # Default: show newest "Found ..." or "Candidate ..." line
    fallback = _find_last_matching(tail, ("Found ", "Candidate ", "Executing "))
    if fallback:
        return "Last activity", _truncate(fallback, 220)
    return "Last activity", _truncate(tail[-1], 220)


def _parse_last_sizing_check(tail: list[str]) -> dict[str, Decimal] | None:
    for raw in reversed(tail):
        m = _SIZING_RE.search(raw)
        if not m:
            continue
        g = m.groupdict()
        return {
            "equity": _safe_decimal(g.get("equity")),
            "riskcap": _safe_decimal(g.get("riskcap")),
            "used": _safe_decimal(g.get("used")),
            "remrisk": _safe_decimal(g.get("remrisk")),
            "liqcap": _safe_decimal(g.get("liqcap")),
            "lcap": _safe_decimal(g.get("lcap")),
            "xcap": _safe_decimal(g.get("xcap")),
            "avail": _safe_decimal(g.get("avail")),
        }
    return None


@dataclass(frozen=True, slots=True)
class DbStats:
    total_trades: int = 0
    closed_trades: int = 0
    open_trades: int = 0
    total_realized_pnl: Decimal = Decimal("0")
    total_funding: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class TradeRow:
    trade_id: str
    symbol: str
    status: str
    execution_state: str
    leg1_exchange: str
    leg1_side: str
    leg2_exchange: str
    leg2_side: str
    target_qty: Decimal
    target_notional_usd: Decimal
    entry_apy: Decimal
    current_apy: Decimal | None
    entry_spread: Decimal
    leg1_entry_price: Decimal
    leg2_entry_price: Decimal
    funding_collected: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    leg1_fees: Decimal
    leg2_fees: Decimal
    total_fees: Decimal
    created_at: datetime | None
    opened_at: datetime | None
    last_eval_at: datetime | None
    close_reason: str | None


@dataclass(frozen=True, slots=True)
class AttemptRow:
    created_at: datetime | None
    symbol: str
    status: str
    stage: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    updated_at: datetime
    db_ok: bool
    db_error: str | None
    stats: DbStats
    trades: list[TradeRow]
    attempts: list[AttemptRow]
    log_ok: bool
    log_path: str | None
    log_age_seconds: float | None
    health: dict[str, str]
    thresholds: dict[str, Decimal | int]
    sizing_check: dict[str, Decimal] | None
    last_decision_title: str
    last_decision_detail: str
    top_opportunity_line: str | None
    log_highlights: list[str]


def _db_connect(db_path: Path) -> sqlite3.Connection:
    # Try read-only first (safer with live bot running).
    try:
        return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0)
    except Exception:
        return sqlite3.connect(str(db_path), timeout=2.0)


def _try_migrate_trades_schema(db_path: Path) -> None:
    """
    Best-effort additive schema migration for dashboard compatibility.

    The bot normally applies schema migrations on startup via the SQLiteStore.
    If the bot wasn't restarted yet, the dashboard would otherwise lack columns
    like `current_apy` / `last_eval_at` and can only show `entry_apy`.
    """
    global _MIGRATION_DONE, _MIGRATION_LAST_ATTEMPT
    if _MIGRATION_DONE:
        return

    now = _now_utc()
    if _MIGRATION_LAST_ATTEMPT and (now - _MIGRATION_LAST_ATTEMPT).total_seconds() < 60:
        return
    _MIGRATION_LAST_ATTEMPT = now

    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        conn.execute("PRAGMA busy_timeout=2000")
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(trades)")
        cols = {row[1] for row in cur.fetchall()}
        stmts: list[str] = []
        if "current_apy" not in cols:
            stmts.append("ALTER TABLE trades ADD COLUMN current_apy DECIMAL DEFAULT '0'")
        if "current_lighter_rate" not in cols:
            stmts.append("ALTER TABLE trades ADD COLUMN current_lighter_rate DECIMAL DEFAULT '0'")
        if "current_x10_rate" not in cols:
            stmts.append("ALTER TABLE trades ADD COLUMN current_x10_rate DECIMAL DEFAULT '0'")
        if "last_eval_at" not in cols:
            stmts.append("ALTER TABLE trades ADD COLUMN last_eval_at TEXT")

        for stmt in stmts:
            conn.execute(stmt)
        if stmts:
            conn.commit()

        cur.execute("PRAGMA table_info(trades)")
        final_cols = {row[1] for row in cur.fetchall()}
        if "current_apy" in final_cols and "last_eval_at" in final_cols:
            _MIGRATION_DONE = True
    except Exception:
        return
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _fetch_db(db_path: Path, *, max_attempts: int) -> tuple[DbStats, list[TradeRow], list[AttemptRow]]:
    conn: sqlite3.Connection | None = None
    try:
        # Try to add missing columns once (best-effort); safe even if it fails/DB locked.
        _try_migrate_trades_schema(db_path)

        conn = _db_connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed_trades,
                SUM(CASE WHEN status IN ('OPEN','OPENING','CLOSING','PENDING') THEN 1 ELSE 0 END) AS open_trades,
                COALESCE(SUM(realized_pnl), 0) AS total_realized_pnl,
                COALESCE(SUM(funding_collected), 0) AS total_funding,
                COALESCE(SUM(leg1_fees + leg2_fees), 0) AS total_fees
            FROM trades
            """
        )
        row = cur.fetchone() or {}
        stats = DbStats(
            total_trades=int(row["total_trades"] or 0),
            closed_trades=int(row["closed_trades"] or 0),
            open_trades=int(row["open_trades"] or 0),
            total_realized_pnl=_safe_decimal(row["total_realized_pnl"]),
            total_funding=_safe_decimal(row["total_funding"]),
            total_fees=_safe_decimal(row["total_fees"]),
        )

        # Best-effort: include current_apy + last_eval_at if the bot schema migration already ran.
        cur.execute("PRAGMA table_info(trades)")
        trade_cols = {row[1] for row in cur.fetchall()}
        has_current = "current_apy" in trade_cols and "last_eval_at" in trade_cols

        if has_current:
            cur.execute(
                """
                SELECT
                    trade_id, symbol, status, execution_state,
                    leg1_exchange, leg1_side, leg2_exchange, leg2_side,
                    target_qty, target_notional_usd,
                    entry_apy, current_apy, entry_spread,
                    leg1_entry_price, leg2_entry_price,
                    funding_collected, unrealized_pnl, realized_pnl,
                    leg1_fees, leg2_fees,
                    (leg1_fees + leg2_fees) AS total_fees,
                    created_at, opened_at, last_eval_at,
                    close_reason
                FROM trades
                WHERE status IN ('OPEN', 'OPENING', 'CLOSING', 'PENDING')
                ORDER BY created_at DESC
                """
            )
        else:
            cur.execute(
                """
                SELECT
                    trade_id, symbol, status, execution_state,
                    leg1_exchange, leg1_side, leg2_exchange, leg2_side,
                    target_qty, target_notional_usd,
                    entry_apy, entry_spread,
                    leg1_entry_price, leg2_entry_price,
                    funding_collected, unrealized_pnl, realized_pnl,
                    leg1_fees, leg2_fees,
                    (leg1_fees + leg2_fees) AS total_fees,
                    created_at, opened_at,
                    close_reason
                FROM trades
                WHERE status IN ('OPEN', 'OPENING', 'CLOSING', 'PENDING')
                ORDER BY created_at DESC
                """
            )
        trades: list[TradeRow] = []
        for r in cur.fetchall():
            trades.append(
                TradeRow(
                    trade_id=str(r["trade_id"]),
                    symbol=str(r["symbol"]),
                    status=str(r["status"]),
                    execution_state=str(r["execution_state"]),
                    leg1_exchange=str(r["leg1_exchange"]),
                    leg1_side=str(r["leg1_side"]),
                    leg2_exchange=str(r["leg2_exchange"]),
                    leg2_side=str(r["leg2_side"]),
                    target_qty=_safe_decimal(r["target_qty"]),
                    target_notional_usd=_safe_decimal(r["target_notional_usd"]),
                    entry_apy=_safe_decimal(r["entry_apy"]),
                    current_apy=_safe_decimal(r["current_apy"]) if has_current else None,
                    entry_spread=_safe_decimal(r["entry_spread"]),
                    leg1_entry_price=_safe_decimal(r["leg1_entry_price"]),
                    leg2_entry_price=_safe_decimal(r["leg2_entry_price"]),
                    funding_collected=_safe_decimal(r["funding_collected"]),
                    unrealized_pnl=_safe_decimal(r["unrealized_pnl"]),
                    realized_pnl=_safe_decimal(r["realized_pnl"]),
                    leg1_fees=_safe_decimal(r["leg1_fees"]),
                    leg2_fees=_safe_decimal(r["leg2_fees"]),
                    total_fees=_safe_decimal(r["total_fees"]),
                    created_at=_parse_iso(r["created_at"]),
                    opened_at=_parse_iso(r["opened_at"]),
                    last_eval_at=_parse_iso(r["last_eval_at"]) if has_current else None,
                    close_reason=r["close_reason"],
                )
            )

        cur.execute(
            """
            SELECT created_at, symbol, status, stage, reason
            FROM execution_attempts
            WHERE status IN ('FAILED','REJECTED')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(max(1, max_attempts)),),
        )
        attempts: list[AttemptRow] = []
        for r in cur.fetchall():
            attempts.append(
                AttemptRow(
                    created_at=_parse_iso(r["created_at"]),
                    symbol=str(r["symbol"]),
                    status=str(r["status"]),
                    stage=str(r["stage"]),
                    reason=r["reason"],
                )
            )

        return stats, trades, attempts
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


def collect_snapshot(
    *,
    db_path: Path,
    log_dir: Path,
    tail_lines: int,
    tail_bytes: int,
    highlight_lines: int,
    max_attempts: int,
) -> Snapshot:
    updated_at = _now_utc()

    # DB
    db_ok = True
    db_err: str | None = None
    stats = DbStats()
    trades: list[TradeRow] = []
    attempts: list[AttemptRow] = []
    try:
        if db_path.exists():
            stats, trades, attempts = _fetch_db(db_path, max_attempts=max_attempts)
        else:
            db_ok = False
            db_err = f"DB not found: {db_path}"
    except Exception as e:
        db_ok = False
        db_err = str(e)

    # Logs
    log_path = _latest_log_path(log_dir)
    log_ok = bool(log_path and log_path.exists())
    log_age_seconds: float | None = None
    health: dict[str, str] = {}
    top_opp_line: str | None = None
    highlights: list[str] = []

    if log_ok and log_path:
        try:
            log_age_seconds = (_now_utc() - datetime.fromtimestamp(log_path.stat().st_mtime, UTC)).total_seconds()
        except Exception:
            log_age_seconds = None

        tail = _tail_lines(log_path, max_bytes=tail_bytes, max_lines=tail_lines)
        health = _parse_health_from_lines(tail)
        top_opp_line = _find_last_matching(tail, ("Found ", "top:"))
        highlights = _highlight_log(tail, max_items=highlight_lines)
        dec_title, dec_detail = _infer_last_decision(tail)
        sizing_check = _parse_last_sizing_check(tail)
    else:
        dec_title, dec_detail = ("No logs", "No log file found in logs/.")
        sizing_check = None

    thresholds = _load_thresholds(Path("src/funding_bot/config/config.yaml"))

    return Snapshot(
        updated_at=updated_at,
        db_ok=db_ok,
        db_error=db_err,
        stats=stats,
        trades=trades,
        attempts=attempts,
        log_ok=log_ok,
        log_path=(log_path.name if log_path else None),
        log_age_seconds=log_age_seconds,
        health=health,
        thresholds=thresholds,
        sizing_check=sizing_check,
        last_decision_title=dec_title,
        last_decision_detail=dec_detail,
        top_opportunity_line=top_opp_line,
        log_highlights=highlights,
    )


def _build_header(s: Snapshot) -> Panel:
    age = s.log_age_seconds
    status = "NO LOG"
    status_style = "bold red"
    if s.log_ok:
        if age is None:
            status = "LOG?"
            status_style = "bold yellow"
        elif age <= 30:
            status = "RUNNING"
            status_style = "bold green"
        else:
            status = "STALE"
            status_style = "bold red"

    paused = s.health.get("paused", "").strip().lower() == "true"
    paused_text = "PAUSED" if paused else "ACTIVE"
    paused_style = "bold red" if paused else "bold green"

    equity = s.health.get("equity", "n/a").strip()
    free_margin = s.health.get("free_margin", "n/a").strip()
    exposure = s.health.get("exposure", "n/a").strip()
    market_data = s.health.get("market_data", "n/a").strip()

    grid = Table.grid(expand=True)
    grid.add_column(ratio=2)
    grid.add_column(ratio=2)
    grid.add_column(justify="right", ratio=2)

    left = Text()
    left.append("Funding Bot Dashboard", style="bold white")
    left.append(f"\nUpdated: {s.updated_at.astimezone().strftime('%H:%M:%S')}", style="dim")

    mid = Text()
    mid.append(f"Status: {status}\n", style=status_style)
    if s.log_path:
        mid.append(f"Log: {s.log_path}\n", style="dim")
    mid.append(f"Trading: {paused_text}", style=paused_style)

    right = Text()
    right.append(f"Equity: {equity}\n", style="bold")
    right.append(f"Free margin: {free_margin}\n", style="bold")
    right.append(f"Exposure: {exposure}\n", style="bold")
    right.append(f"Market data: {market_data}", style="bold")

    grid.add_row(left, mid, right)
    return Panel(grid, box=box.ROUNDED, border_style="blue")


def _trades_table(trades: list[TradeRow]) -> Table:
    t = Table(box=box.MINIMAL_DOUBLE_HEAD, expand=True)
    t.add_column("Symbol", style="bold")
    t.add_column("Status")
    t.add_column("Qty", justify="right")
    t.add_column("Notional", justify="right")
    t.add_column("APY(now/entry)", justify="right")
    t.add_column("Hold(h)", justify="right")
    t.add_column("Funding", justify="right")
    t.add_column("uPnL", justify="right")
    t.add_column("Fees(L/X)", justify="right")
    t.add_column("L1/L2", justify="left")

    now = _now_utc()
    for tr in trades:
        hold_h = ""
        if tr.opened_at:
            hold_h = f"{(now - tr.opened_at).total_seconds() / 3600:.1f}"
        apy_now = tr.current_apy
        apy_entry = tr.entry_apy
        apy_stale = False
        apy_stale_cond = (apy_now is None) or (tr.last_eval_at is None) or (now - tr.last_eval_at).total_seconds() > 120
        if apy_stale_cond:
            apy_stale = True
        if tr.status == "OPEN":
            status_style = "green"
        elif tr.status in ("OPENING", "CLOSING"):
            status_style = "yellow"
        else:
            status_style = "magenta"
        upnl_style = "green" if tr.unrealized_pnl >= 0 else "red"
        apy_style = "dim" if apy_stale else ""
        apy_text = "--"
        if (apy_now is not None) and (tr.last_eval_at is not None):
            apy_text = f"{(apy_now * Decimal('100')):.1f}"
        t.add_row(
            tr.symbol,
            Text(tr.status, style=status_style),
            f"{tr.target_qty:.4f}",
            f"${tr.target_notional_usd:.0f}",
            Text(f"{apy_text}/{(apy_entry * Decimal('100')):.1f}%", style=apy_style),
            hold_h or "-",
            f"${tr.funding_collected:.2f}",
            Text(f"${tr.unrealized_pnl:.2f}", style=upnl_style),
            f"${tr.leg1_fees:.2f}/${tr.leg2_fees:.2f}",
            f"{tr.leg1_exchange}:{tr.leg1_side} / {tr.leg2_exchange}:{tr.leg2_side}",
        )

    if not trades:
        t.add_row("-", "-", "-", "-", "-", "-", "-", "-", "-", "-")
    return t


def _kpis_panel(s: Snapshot) -> Panel:
    stats = s.stats
    realized = stats.total_realized_pnl
    funding = stats.total_funding
    fees = stats.total_fees
    net = funding - fees + realized  # rough: realized already includes fees in trade.pnl, but still useful indicator

    body = Text()
    body.append("DB\n", style="bold underline")
    if s.db_ok:
        body.append(f"Trades: {stats.total_trades} (open {stats.open_trades}, closed {stats.closed_trades})\n")
        body.append("Realized PnL: ", style="bold")
        body.append(f"${realized:.2f}\n", style=("green" if realized >= 0 else "red"))
        body.append(f"Funding: ${funding:.2f}\n", style="green")
        body.append(f"Fees: -${fees:.2f}\n", style="red")
        body.append("Net (rough): ", style="bold")
        body.append(f"${net:.2f}\n", style=("green" if net >= 0 else "red"))
    else:
        body.append(f"DB ERROR: {_truncate(s.db_error or 'unknown', 160)}\n", style="bold red")

    return Panel(body, title="KPIs", border_style="green", box=box.ROUNDED)


def _risk_panel(s: Snapshot) -> Panel:
    thr = s.thresholds
    max_open = int(thr.get("max_open_trades", 0) or 0)
    max_exposure_pct = _safe_decimal(thr.get("max_exposure_pct", Decimal("0")))
    min_free_margin_pct = _safe_decimal(thr.get("min_free_margin_pct", Decimal("0")))
    max_drawdown_pct = _safe_decimal(thr.get("max_drawdown_pct", Decimal("0")))
    leverage_multiplier = _safe_decimal(thr.get("leverage_multiplier", Decimal("1")))

    trades_n = len(s.trades)

    equity_f = _parse_float(s.health.get("equity", ""))
    exposure_f = _parse_float(s.health.get("exposure", ""))
    free_margin = _parse_percent(s.health.get("free_margin", ""))  # fraction (0..1)
    balances_ok = s.health.get("balances_ok", "n/a").strip().lower()
    balance_error = s.health.get("balance_error", "").strip()
    paused = s.health.get("paused", "").strip().lower() == "true"

    exposure_pct: float | None = None
    if equity_f is not None and exposure_f is not None and equity_f > 0:
        exposure_pct = (exposure_f / equity_f) * 100.0

    # RiskCap logic used by the bot:
    # global_risk_cap = equity * (max_exposure_pct/100) * eff_lev_lighter
    # We show best-available:
    sizing = s.sizing_check or {}
    riskcap = sizing.get("riskcap")
    _used = sizing.get("used")  # Reserved for future sizing display
    liqcap = sizing.get("liqcap")
    avail = sizing.get("avail")
    lcap = sizing.get("lcap")
    xcap = sizing.get("xcap")

    riskcap_approx: float | None = None
    if equity_f is not None and float(max_exposure_pct) > 0 and float(leverage_multiplier) > 0:
        riskcap_approx = equity_f * (float(max_exposure_pct) / 100.0) * float(leverage_multiplier)

    over_risk_cap = False
    if exposure_f is not None:
        if riskcap is not None and riskcap > 0:
            over_risk_cap = exposure_f >= float(riskcap)
        elif riskcap_approx is not None and riskcap_approx > 0:
            over_risk_cap = exposure_f >= riskcap_approx

    riskcap_value: float | None = None
    if riskcap is not None and riskcap > 0:
        riskcap_value = float(riskcap)
    elif riskcap_approx is not None and riskcap_approx > 0:
        riskcap_value = float(riskcap_approx)

    risk_util_pct: float | None = None
    if exposure_f is not None and riskcap_value and riskcap_value > 0:
        risk_util_pct = (exposure_f / riskcap_value) * 100.0

    flags: list[str] = []
    if max_open and trades_n >= max_open:
        flags.append("MAX_TRADES")
    if over_risk_cap:
        flags.append("RISKCAP_LIMIT")
    elif risk_util_pct is not None and risk_util_pct >= 90.0:
        flags.append("RISKCAP_NEAR")
    if free_margin is not None and free_margin < min_free_margin_pct:
        flags.append("LOW_FREE_MARGIN")
    if balances_ok == "false":
        flags.append("BALANCE_API")
    if paused:
        flags.append("PAUSED")

    status_style = "green" if not flags else "red"
    status = "OK" if not flags else " | ".join(flags)

    body = Text()
    body.append(f"Status: {status}\n", style=f"bold {status_style}")
    body.append(f"Trades: {trades_n}/{max_open if max_open else '-'}\n")
    if exposure_pct is None:
        body.append("Exposure: n/a\n")
    else:
        exp_line = f"Exposure: ${exposure_f:.2f} ({exposure_pct:.1f}% equity)"
        if risk_util_pct is not None:
            exp_line += f" | {risk_util_pct:.1f}% cap"
        body.append(exp_line + "\n")

    if riskcap is not None and riskcap > 0:
        body.append(f"RiskCap: ${riskcap:.2f} (from bot)\n")
    elif riskcap_approx is not None:
        body.append(f"RiskCap: ${riskcap_approx:.2f} (approx: equity*{max_exposure_pct:.0f}%*{leverage_multiplier}x)\n")

    if free_margin is None:
        body.append("Free margin: n/a\n")
    else:
        body.append(f"Free margin: {free_margin:.2%} (min {min_free_margin_pct:.2%})\n")

    body.append(
        f"Limits: max_exposure={Decimal(str(max_exposure_pct)):.0f}%, "
        f"lev={leverage_multiplier}x, max_dd={Decimal(str(max_drawdown_pct)):.0%}\n"
    )

    if liqcap is not None and liqcap > 0:
        bottleneck = "LIGHTER" if (lcap is not None and xcap is not None and lcap <= xcap) else "X10"
        body.append(f"LiquidityCap: ${liqcap:.2f} ({bottleneck}) | NewAvail: ${avail:.2f}\n")
    if balances_ok == "false" and balance_error:
        body.append(f"Balance error: {_truncate(balance_error, 140)}\n", style="yellow")

    return Panel(body, title="Risk", border_style="yellow", box=box.ROUNDED)


def _decision_panel(s: Snapshot) -> Panel:
    body = Text()
    body.append(f"{s.last_decision_title}\n", style="bold underline")
    body.append(s.last_decision_detail, style="cyan")
    return Panel(body, title="Why no trade started?", border_style="blue", box=box.ROUNDED)


def _attempts_table(attempts: list[AttemptRow]) -> Table:
    t = Table(box=box.SIMPLE_HEAVY, expand=True)
    t.add_column("Age", justify="right")
    t.add_column("Symbol", style="bold")
    t.add_column("Status")
    t.add_column("Stage")
    t.add_column("Reason")

    now = _now_utc()
    for a in attempts[:25]:
        age = "-"
        if a.created_at:
            age = f"{(now - a.created_at).total_seconds() / 60:.0f}m"
        style = "red" if a.status == "FAILED" else "yellow"
        t.add_row(
            age,
            a.symbol,
            Text(a.status, style=style),
            _truncate(a.stage, 22),
            _truncate(a.reason or "", 60),
        )
    if not attempts:
        t.add_row("-", "-", "-", "-", "-")
    return t


def _monitor_panel(s: Snapshot) -> Panel:
    blocks: list[Any] = []

    top = s.top_opportunity_line or "No opportunity line found yet."
    blocks.append(Text("Latest opportunity (from logs)", style="bold underline"))
    blocks.append(Text(_truncate(top, 220), style="cyan"))

    blocks.append(Text(""))
    blocks.append(Text("Highlights", style="bold underline"))
    if s.log_highlights:
        for line in s.log_highlights:
            style = "red" if "[ERROR]" in line or "CRITICAL" in line else ("yellow" if "[WARNING]" in line else "dim")
            blocks.append(Text(_truncate(line, 240), style=style))
    else:
        blocks.append(Text("No highlights in recent log tail.", style="dim"))

    return Panel(Group(*blocks), title="Monitor", border_style="magenta", box=box.ROUNDED)


def _build_layout(s: Snapshot) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=5),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="right", ratio=2),
    )
    layout["right"].split_column(
        Layout(name="risk", size=10),
        Layout(name="kpis", size=10),
        Layout(name="decision", size=6),
        Layout(name="attempts", ratio=1),
        Layout(name="monitor", ratio=1),
    )

    layout["header"].update(_build_header(s))
    layout["left"].update(Panel(_trades_table(s.trades), title=f"Active Trades ({len(s.trades)})", border_style="cyan"))
    layout["risk"].update(_risk_panel(s))
    layout["kpis"].update(_kpis_panel(s))
    layout["decision"].update(_decision_panel(s))
    layout["attempts"].update(
        Panel(_attempts_table(s.attempts), title="Recent FAILED/REJECTED executions", border_style="red")
    )
    layout["monitor"].update(_monitor_panel(s))
    layout["footer"].update(Align.center(Text("Ctrl+C to quit", style="dim")))
    return layout


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Realtime TUI dashboard for funding-bot")
    p.add_argument("--db", default="data/funding_v2.db", help="Path to SQLite db")
    p.add_argument("--logs", default="logs", help="Log directory")
    p.add_argument("--refresh", type=float, default=15.0, help="Refresh interval seconds")
    p.add_argument("--tail-lines", type=int, default=200, help="Log lines to scan each refresh")
    p.add_argument("--tail-bytes", type=int, default=64 * 1024, help="Max bytes to read from the end of log")
    p.add_argument("--highlights", type=int, default=8, help="Max highlight lines to show")
    p.add_argument("--attempts", type=int, default=20, help="Max failed/rejected attempts to show")
    p.add_argument("--no-screen", action="store_true", help="Do not use alternate screen buffer")
    p.add_argument("--once", action="store_true", help="Render one snapshot and exit")
    return p.parse_args()


def run() -> None:
    args = parse_args()
    db_path = Path(args.db)
    log_dir = Path(args.logs)

    if args.once:
        snap = collect_snapshot(
            db_path=db_path,
            log_dir=log_dir,
            tail_lines=int(args.tail_lines),
            tail_bytes=int(args.tail_bytes),
            highlight_lines=int(args.highlights),
            max_attempts=int(args.attempts),
        )
        console.print(_build_layout(snap))
        return

    with Live(screen=not args.no_screen, refresh_per_second=4, console=console) as live:
        while True:
            snap = collect_snapshot(
                db_path=db_path,
                log_dir=log_dir,
                tail_lines=int(args.tail_lines),
                tail_bytes=int(args.tail_bytes),
                highlight_lines=int(args.highlights),
                max_attempts=int(args.attempts),
            )
            live.update(_build_layout(snap))
            time.sleep(max(0.2, float(args.refresh)))


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        run()
