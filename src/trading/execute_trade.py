import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


async def execute_trade(
    symbol: str,
    side: str,
    notional_usd: float,
    lighter_adapter,
    x10_adapter,
) -> bool:
    """Pre-flight guarded trade execution.

    Ensures both Lighter and X10 prices are valid (> 0) BEFORE any order.
    Skips entire trade if either price invalid to avoid unhedged positions.
    """
    # Fetch prices from adapters
    lighter_price = lighter_adapter.get_price(symbol)
    try:
        x10_price = x10_adapter.get_price(symbol)
    except AttributeError:
        # Fallback if x10_adapter uses async fetcher
        x10_price = None
        if hasattr(x10_adapter, "fetch_mark_price"):
            try:
                x10_price = await x10_adapter.fetch_mark_price(symbol)
            except Exception:
                x10_price = None

    lp = float(lighter_price) if lighter_price is not None else 0.0
    xp = float(x10_price) if x10_price is not None else 0.0

    if lp <= 0 or xp <= 0:
        logger.error(f"❌ Skipping {symbol}: Invalid prices (L={lp}, X={xp})")
        return False

    # Proceed only if both prices are valid
    side_norm = "BUY" if str(side).upper() == "BUY" else "SELL"

    try:
        # Safer sequential: Lighter first to ensure hedge acceptance
        ok_lighter, lighter_tx = await lighter_adapter.open_live_position(
            symbol=symbol,
            side=side_norm,
            notional_usd=notional_usd,
            price=lp,
        )
        if not ok_lighter:
            logger.error(f"❌ Lighter leg failed for {symbol}; aborting X10 leg")
            return False

        ok_x10, x10_tx = await x10_adapter.open_live_position(
            symbol=symbol,
            side=side_norm,
            notional_usd=notional_usd,
            price=xp,
        )
        if not ok_x10:
            logger.error(f"❌ X10 leg failed for {symbol}; consider compensating close on Lighter")
            return False

        logger.info(f"✅ Hedged trade executed for {symbol} (Lighter={lighter_tx}, X10={x10_tx})")
        return True
    except Exception as e:
        logger.error(f"❌ execute_trade error for {symbol}: {e}")
        return False
