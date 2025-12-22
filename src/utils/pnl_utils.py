# ═══════════════════════════════════════════════════════════════════════════════
# PNL UTILS - Robust Realized PnL Computation
# ═══════════════════════════════════════════════════════════════════════════════
# Features:
# ✓ Accurate price PnL computation for LONG/SHORT positions
# ✓ Fee deduction from fills (taker/maker)
# ✓ CSV reconciliation for Lighter trade exports
# ✓ Sign-correct funding attribution
# ═══════════════════════════════════════════════════════════════════════════════

import csv
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert value to float."""
    if val is None:
        return default
    try:
        if isinstance(val, (int, float)):
            return float(val)
        return float(str(val).strip() or default)
    except (ValueError, TypeError):
        return default


def _safe_decimal(val: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Safely convert value to Decimal."""
    if val is None:
        return default
    try:
        if isinstance(val, Decimal):
            return val
        return Decimal(str(val))
    except Exception:
        return default


def _side_sign(side: Optional[str]) -> int:
    """
    Return +1 for LONG/BUY, -1 for SHORT/SELL, 0 for unknown.
    
    Convention:
    - LONG/BUY = +1 (profit when price goes up)
    - SHORT/SELL = -1 (profit when price goes down)
    """
    if not side:
        return 0
    s = str(side).upper()
    if "BUY" in s or "LONG" in s:
        return 1
    if "SELL" in s or "SHORT" in s:
        return -1
    return 0


def compute_realized_pnl(
    symbol: str,
    side: str,
    entry_fills: List[Dict[str, Any]],
    close_fills: List[Dict[str, Any]],
    fee_currency: str = "USD",
) -> Dict[str, Decimal]:
    """
    Compute realized PnL from entry and close fills.
    
    This function computes the ACTUAL realized PnL by:
    1. Computing volume-weighted average entry and close prices
    2. Computing price PnL based on position side
    3. Subtracting total fees from both entry and close
    
    ALL RETURNS ARE DECIMAL for precision-critical financial calculations.
    
    Args:
        symbol: Trading pair symbol (e.g., "BTC-USD")
        side: Position side ("LONG" or "SHORT")
        entry_fills: List of entry fills, each with {price, qty, fee, is_taker}
        close_fills: List of close fills, each with {price, qty, fee, is_taker}
        fee_currency: Currency for fee normalization (default: "USD")
    
    Returns:
        Dict[str, Decimal] with {price_pnl, fee_total, total_pnl, entry_vwap, close_vwap, qty}
        
    Example:
        >>> entry_fills = [{"price": 0.4054, "qty": 197, "fee": 0.000225, "is_taker": True}]
        >>> close_fills = [{"price": 0.4052, "qty": 197, "fee": 0.000225, "is_taker": True}]
        >>> result = compute_realized_pnl("TAO-USD", "LONG", entry_fills, close_fills)
        >>> result["price_pnl"]  # Decimal('-0.0394')
        >>> result["total_pnl"]  # Decimal('-0.03985')
    """
    sign = _side_sign(side)
    
    if sign == 0:
        logger.warning(f"[PNL] {symbol}: Unknown side '{side}', cannot compute PnL")
        return {"price_pnl": 0.0, "fee_total": 0.0, "total_pnl": 0.0}
    
    # Compute entry VWAP and total qty
    entry_value = Decimal("0")
    entry_qty = Decimal("0")
    entry_fee = Decimal("0")
    
    for fill in entry_fills:
        price = _safe_decimal(fill.get("price", 0))
        qty = _safe_decimal(fill.get("qty", 0))
        fee = _safe_decimal(fill.get("fee", 0))
        
        if price > 0 and qty > 0:
            entry_value += price * qty
            entry_qty += qty
        entry_fee += abs(fee)  # Fees are always costs (positive)
    
    entry_vwap = (entry_value / entry_qty) if entry_qty > 0 else Decimal("0")
    
    # Compute close VWAP and total qty
    close_value = Decimal("0")
    close_qty = Decimal("0")
    close_fee = Decimal("0")
    
    for fill in close_fills:
        price = _safe_decimal(fill.get("price", 0))
        qty = _safe_decimal(fill.get("qty", 0))
        fee = _safe_decimal(fill.get("fee", 0))
        
        if price > 0 and qty > 0:
            close_value += price * qty
            close_qty += qty
        close_fee += abs(fee)
    
    close_vwap = (close_value / close_qty) if close_qty > 0 else Decimal("0")
    
    # Use the minimum of entry/close qty for PnL calculation
    # (handles partial closes correctly)
    qty_for_pnl = min(entry_qty, close_qty)
    
    # Compute price PnL
    # LONG: profit = (close_price - entry_price) * qty
    # SHORT: profit = (entry_price - close_price) * qty
    if sign > 0:  # LONG
        price_pnl = (close_vwap - entry_vwap) * qty_for_pnl
    else:  # SHORT
        price_pnl = (entry_vwap - close_vwap) * qty_for_pnl
    
    # Total fees
    fee_total = entry_fee + close_fee
    
    # Total PnL = price PnL - fees
    total_pnl = price_pnl - fee_total
    
    logger.debug(
        f"[PNL] {symbol} ({side}): "
        f"entry_vwap=${float(entry_vwap):.6f}, close_vwap=${float(close_vwap):.6f}, "
        f"qty={float(qty_for_pnl):.4f}, price_pnl=${float(price_pnl):.6f}, "
        f"fees=${float(fee_total):.6f}, total=${float(total_pnl):.6f}"
    )
    
    # Return Decimal values for precision-critical calculations
    return {
        "price_pnl": price_pnl,
        "fee_total": fee_total,
        "total_pnl": total_pnl,
        "entry_vwap": entry_vwap,
        "close_vwap": close_vwap,
        "qty": qty_for_pnl,
    }


def compute_hedge_pnl(
    symbol: str,
    lighter_side: str,
    x10_side: str,
    lighter_entry_price: float,
    lighter_close_price: float,
    lighter_qty: float,
    x10_entry_price: float,
    x10_close_price: float,
    x10_qty: float,
    lighter_fees: float = 0.0,
    x10_fees: float = 0.0,
    funding_collected: float = 0.0,
) -> Dict[str, Decimal]:
    """
    Compute combined realized PnL for a hedged position (Lighter + X10).
    
    In a funding arbitrage strategy, we typically have opposite positions:
    - LONG on one exchange, SHORT on the other
    
    This function computes the TOTAL PnL across both legs.
    
    ALL RETURNS ARE DECIMAL for precision-critical financial calculations.
    
    Args:
        symbol: Trading pair symbol
        lighter_side: Position side on Lighter ("LONG" or "SHORT")
        x10_side: Position side on X10 ("LONG" or "SHORT")
        lighter_entry_price: Lighter entry price
        lighter_close_price: Lighter close price
        lighter_qty: Lighter position quantity
        x10_entry_price: X10 entry price
        x10_close_price: X10 close price
        x10_qty: X10 position quantity
        lighter_fees: Total Lighter fees (entry + exit)
        x10_fees: Total X10 fees (entry + exit)
        funding_collected: Net funding collected (profit-positive)
    
    Returns:
        Dict[str, Decimal] with {lighter_pnl, x10_pnl, price_pnl_total, fee_total, funding, total_pnl}
    """
    # Convert all inputs to Decimal for precision
    d_lighter_entry = _safe_decimal(lighter_entry_price)
    d_lighter_close = _safe_decimal(lighter_close_price)
    d_lighter_qty = _safe_decimal(lighter_qty)
    d_x10_entry = _safe_decimal(x10_entry_price)
    d_x10_close = _safe_decimal(x10_close_price)
    d_x10_qty = _safe_decimal(x10_qty)
    d_lighter_fees = _safe_decimal(abs(lighter_fees))
    d_x10_fees = _safe_decimal(abs(x10_fees))
    d_funding = _safe_decimal(funding_collected)
    
    # Compute Lighter leg PnL
    lighter_sign = _side_sign(lighter_side)
    if lighter_sign > 0:  # LONG
        lighter_price_pnl = (d_lighter_close - d_lighter_entry) * d_lighter_qty
    elif lighter_sign < 0:  # SHORT
        lighter_price_pnl = (d_lighter_entry - d_lighter_close) * d_lighter_qty
    else:
        lighter_price_pnl = Decimal("0")
    
    # Compute X10 leg PnL
    x10_sign = _side_sign(x10_side)
    if x10_sign > 0:  # LONG
        x10_price_pnl = (d_x10_close - d_x10_entry) * d_x10_qty
    elif x10_sign < 0:  # SHORT
        x10_price_pnl = (d_x10_entry - d_x10_close) * d_x10_qty
    else:
        x10_price_pnl = Decimal("0")
    
    # Combined values (all Decimal)
    price_pnl_total = lighter_price_pnl + x10_price_pnl
    fee_total = d_lighter_fees + d_x10_fees
    total_pnl = price_pnl_total - fee_total + d_funding
    
    logger.debug(
        f"[PNL] {symbol} Hedge: "
        f"lighter_pnl=${float(lighter_price_pnl):.6f} ({lighter_side}), "
        f"x10_pnl=${float(x10_price_pnl):.6f} ({x10_side}), "
        f"fees=${float(fee_total):.6f}, funding=${float(d_funding):.6f}, "
        f"total=${float(total_pnl):.6f}"
    )
    
    return {
        "lighter_pnl": lighter_price_pnl,
        "x10_pnl": x10_price_pnl,
        "price_pnl_total": price_pnl_total,
        "fee_total": fee_total,
        "funding": d_funding,
        "total_pnl": total_pnl,
    }


def sum_closed_pnl_from_csv(csv_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Sum closed PnL from Lighter trade export CSV rows.
    
    The Lighter CSV export has a "Closed PnL" column that is populated
    when a position is closed (Side = "Close Long" or "Close Short").
    
    Args:
        csv_rows: List of dicts representing CSV rows with keys:
                  - Market: Symbol (e.g., "TAO")
                  - Side: "Buy", "Sell", "Close Long", "Close Short"
                  - Closed PnL: PnL value for close trades (may be "-" or empty)
                  - Date, Size, Price, Trade Value (optional)
    
    Returns:
        Dict mapping symbol to {total_pnl, trade_count, trades}
        
    Example:
        >>> rows = [
        ...     {"Market": "TAO", "Side": "Close Short", "Closed PnL": "-0.0123"},
        ...     {"Market": "TAO", "Side": "Close Short", "Closed PnL": "0.0456"},
        ... ]
        >>> result = sum_closed_pnl_from_csv(rows)
        >>> result["TAO-USD"]["total_pnl"]
        0.0333
    """
    results: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total_pnl": 0.0,
        "trade_count": 0,
        "trades": [],
    })
    
    for row in csv_rows:
        market = row.get("Market", "")
        side = row.get("Side", "")
        closed_pnl_str = row.get("Closed PnL", "")
        
        # Skip non-close trades
        if "Close" not in side:
            continue
        
        # Skip empty or dash PnL values
        if not closed_pnl_str or closed_pnl_str == "-":
            continue
        
        try:
            pnl_value = float(closed_pnl_str)
        except (ValueError, TypeError):
            continue
        
        # Normalize symbol to match bot format (TAO -> TAO-USD)
        symbol = market
        if symbol and not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"
        
        results[symbol]["total_pnl"] += pnl_value
        results[symbol]["trade_count"] += 1
        results[symbol]["trades"].append({
            "date": row.get("Date", ""),
            "side": side,
            "pnl": pnl_value,
            "size": _safe_float(row.get("Size", 0)),
            "price": _safe_float(row.get("Price", 0)),
            "trade_value": _safe_float(row.get("Trade Value", 0)),
        })
    
    return dict(results)


def parse_csv_file(csv_path: str) -> List[Dict[str, Any]]:
    """
    Parse a Lighter trade export CSV file.
    
    Args:
        csv_path: Path to the CSV file
    
    Returns:
        List of row dicts
    """
    rows = []
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        logger.error(f"[PNL] Failed to parse CSV {csv_path}: {e}")
    return rows


def reconcile_csv_vs_db(
    csv_pnl: Dict[str, Dict[str, Any]],
    db_pnl: Dict[str, float],
    tolerance: float = 0.01,
) -> Dict[str, Dict[str, Any]]:
    """
    Compare CSV closed PnL against DB recorded PnL per symbol.
    
    Args:
        csv_pnl: Dict from sum_closed_pnl_from_csv
        db_pnl: Dict mapping symbol to recorded PnL from DB
        tolerance: Absolute difference threshold for mismatch (default: $0.01)
    
    Returns:
        Dict with {matches, mismatches, missing_in_db, missing_in_csv}
    """
    all_symbols = set(csv_pnl.keys()) | set(db_pnl.keys())
    
    matches = []
    mismatches = []
    missing_in_db = []
    missing_in_csv = []
    
    for symbol in sorted(all_symbols):
        csv_val = csv_pnl.get(symbol, {}).get("total_pnl", 0.0)
        db_val = db_pnl.get(symbol, 0.0)
        
        in_csv = symbol in csv_pnl
        in_db = symbol in db_pnl
        
        if in_csv and in_db:
            diff = abs(csv_val - db_val)
            if diff < tolerance:
                matches.append({
                    "symbol": symbol,
                    "csv_pnl": csv_val,
                    "db_pnl": db_val,
                })
            else:
                mismatches.append({
                    "symbol": symbol,
                    "csv_pnl": csv_val,
                    "db_pnl": db_val,
                    "difference": csv_val - db_val,
                })
        elif in_csv and not in_db:
            missing_in_db.append({
                "symbol": symbol,
                "csv_pnl": csv_val,
            })
        elif in_db and not in_csv:
            missing_in_csv.append({
                "symbol": symbol,
                "db_pnl": db_val,
            })
    
    return {
        "matches": matches,
        "mismatches": mismatches,
        "missing_in_db": missing_in_db,
        "missing_in_csv": missing_in_csv,
    }


def normalize_funding_sign(
    funding_paid_out: float,
    is_profit_positive: bool = False,
) -> float:
    """
    Normalize funding sign to profit-positive convention.
    
    Different APIs use different conventions:
    - Lighter: total_funding_paid_out > 0 means you PAID (cost)
    - Our convention: funding > 0 means you RECEIVED (profit)
    
    Args:
        funding_paid_out: Raw funding value from API
        is_profit_positive: If True, value is already profit-positive
    
    Returns:
        Profit-positive funding value (positive = received, negative = paid)
    """
    if is_profit_positive:
        return funding_paid_out
    # Invert: paid_out > 0 is cost, so negate for profit-positive
    return -funding_paid_out



