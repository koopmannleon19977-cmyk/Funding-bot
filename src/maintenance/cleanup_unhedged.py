import os
import sys
import logging
from typing import Dict, List, Set, Tuple

# Assumed local adapters; adjust imports to your project layout
try:
    from src.adapters.lighter_adapter import LighterAdapter
except ImportError:
    # Fallback if running as a module from project root
    from adapters.lighter_adapter import LighterAdapter

try:
    # x10 python sdk
    from x10 import X10Client
except ImportError:
    X10Client = None  # Will error later with a helpful message


logger = logging.getLogger("cleanup_unhedged")

from src.utils import safe_float


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def _init_x10_client() -> "X10Client":
    """Initialize X10 SDK client from environment variables.

    Required env vars:
      - X10_API_KEY
      - X10_API_SECRET
      - X10_BASE_URL (optional; default production)
    """
    if X10Client is None:
        raise RuntimeError(
            "x10 python_sdk not installed. Install via 'pip install git+https://github.com/x10xchange/python_sdk.git'"
        )

    api_key = os.getenv("X10_API_KEY")
    api_secret = os.getenv("X10_API_SECRET")
    base_url = os.getenv("X10_BASE_URL", "https://api.x10xchange.com")

    if not api_key or not api_secret:
        raise RuntimeError("Missing X10 credentials in env: X10_API_KEY / X10_API_SECRET")

    return X10Client(api_key=api_key, api_secret=api_secret, base_url=base_url)


def _init_lighter_adapter() -> LighterAdapter:
    """Initialize Lighter adapter. Assumes adapter reads env for credentials."""
    return LighterAdapter()


def _fetch_x10_positions(x10_client: "X10Client") -> Dict[str, Dict]:
    """Fetch all X10 positions and return a mapping by symbol.

    Returns: { symbol: position_dict }
    Position dict expected keys (based on SDK): symbol, size, entry_price, pnl, ...
    """
    positions: List[Dict] = x10_client.get_positions()
    by_symbol = {p["symbol"]: p for p in positions}
    return by_symbol


def _fetch_lighter_positions(lighter: LighterAdapter) -> Set[str]:
    """Fetch all Lighter positions and return a set of symbols currently hedged."""
    positions = lighter.get_positions()
    # Normalize to symbol strings
    symbols = set()
    for p in positions:
        sym = p.get("symbol") or p.get("asset") or p.get("pair")
        if sym:
            symbols.add(str(sym))
    return symbols


def _identify_unhedged(x10_by_symbol: Dict[str, Dict], lighter_symbols: Set[str]) -> List[Tuple[str, Dict]]:
    """Return list of (symbol, position) for positions present in X10 but not in Lighter."""
    unhedged = []
    for symbol, pos in x10_by_symbol.items():
        if symbol not in lighter_symbols:
            unhedged.append((symbol, pos))
    return unhedged


def _close_x10_position(x10_client: "X10Client", symbol: str, position: Dict) -> Dict:
    """Close X10 position via market order using reduce_only.

    Determines side opposite to current position size.
    Returns the resulting order/trade response.
    """
    size = safe_float(position.get("size") or position.get("quantity"), 0.0)
    if size is None:
        raise RuntimeError(f"Position for {symbol} missing size")

    try:
        size_val = float(size)
    except Exception:
        raise RuntimeError(f"Position size for {symbol} not numeric: {size}")

    if size_val == 0:
        return {"status": "skipped", "reason": "zero size"}

    side = "sell" if size_val > 0 else "buy"
    qty = abs(size_val)

    resp = x10_client.place_market_order(
        symbol=symbol,
        side=side,
        quantity=qty,
        reduce_only=True,
    )
    return resp


def _extract_pnl(position: Dict) -> float:
    pnl = safe_float(position.get("pnl") or position.get("unrealized_pnl"), 0.0)
    try:
        return float(pnl)
    except Exception:
        return 0.0


def cleanup_unhedged_positions() -> None:
    """Emergency cleanup: close all X10 positions that are not hedged on Lighter.

    Steps:
      1. Fetch X10 positions
      2. Fetch Lighter positions
      3. Identify unhedged symbols
      4. Close each via market order (reduce_only)
      5. Log each closure with PnL
    """
    x10 = _init_x10_client()
    lighter = _init_lighter_adapter()

    x10_positions = _fetch_x10_positions(x10)
    lighter_symbols = _fetch_lighter_positions(lighter)

    unhedged = _identify_unhedged(x10_positions, lighter_symbols)

    if not unhedged:
        logger.info("No unhedged positions found. Nothing to do.")
        return

    logger.info(f"Found {len(unhedged)} unhedged positions. Starting closures...")

    for symbol, pos in unhedged:
        pnl = _extract_pnl(pos)
        size = safe_float(pos.get("size"), 0.0)
        logger.info(f"Closing {symbol}: size={size}, pre-close PnL={pnl}")
        try:
            resp = _close_x10_position(x10, symbol, pos)
            logger.info(f"Closed {symbol}. Response: {resp}")
        except Exception as e:
            logger.error(f"Failed closing {symbol}: {e}")


def main():
    try:
        cleanup_unhedged_positions()
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
