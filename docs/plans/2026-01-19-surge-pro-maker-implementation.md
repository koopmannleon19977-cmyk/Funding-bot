# Surge Pro Maker Strategy - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement Maker/Maker order strategy for Surge Pro to achieve 0% fees and break-even volume farming.

**Architecture:** Extend existing SurgeProEngine with MakerExecutor component that places POST_ONLY orders, monitors fills with timeout, reprices on non-fill, and falls back to IOC taker for exits. Add RiskGuard for 24/7 anomaly detection.

**Tech Stack:** Python 3.12, asyncio, X10 SDK (POST_ONLY support already exists), SQLite for state

**Design Doc:** `docs/plans/2026-01-19-surge-pro-volume-farming-design.md`

---

## Task 1: Add Maker-Mode Settings

**Files:**
- Modify: `src/funding_bot/config/settings.py:105-131` (SurgeProSettings class)

**Step 1: Write failing test**

Create: `tests/unit/config/test_surge_pro_maker_settings.py`

```python
"""Tests for Surge Pro maker mode settings."""

import pytest
from funding_bot.config.settings import SurgeProSettings


def test_maker_mode_default():
    """Maker mode should default to 'taker' for backwards compatibility."""
    settings = SurgeProSettings()
    assert settings.order_mode == "taker"


def test_maker_mode_can_be_set():
    """Maker mode can be set to 'maker'."""
    settings = SurgeProSettings(order_mode="maker")
    assert settings.order_mode == "maker"


def test_maker_entry_timeout_default():
    """Entry fill timeout should have sensible default."""
    settings = SurgeProSettings()
    assert settings.maker_entry_timeout_s == 2.0


def test_maker_exit_timeout_default():
    """Exit fill timeout should have sensible default."""
    settings = SurgeProSettings()
    assert settings.maker_exit_timeout_s == 1.5


def test_maker_exit_max_retries_default():
    """Max exit retries before taker fallback."""
    settings = SurgeProSettings()
    assert settings.maker_exit_max_retries == 3


def test_symbols_whitelist():
    """Symbols whitelist for liquid markets."""
    settings = SurgeProSettings(
        symbols=["BTC", "ETH", "SOL"]
    )
    assert settings.symbols == ["BTC", "ETH", "SOL"]
```

**Step 2: Run test to verify it fails**

```bash
cd /home/koopm/funding-bot/.worktrees/surge-pro-maker
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/config/test_surge_pro_maker_settings.py -v
```

Expected: FAIL with AttributeError (order_mode not defined)

**Step 3: Implement settings**

Modify `src/funding_bot/config/settings.py`, add to SurgeProSettings class:

```python
class SurgeProSettings(BaseModel):
    """Settings for Surge Pro strategy."""

    # === Existing settings (keep all) ===
    scan_interval_seconds: Decimal = Decimal("1.0")
    symbol_limit: int = 10
    symbol_refresh_seconds: Decimal = Decimal("60.0")
    depth_levels: int = 20
    entry_imbalance_threshold: Decimal = Decimal("0.30")
    exit_imbalance_threshold: Decimal = Decimal("0.10")
    stop_loss_bps: Decimal = Decimal("20")
    take_profit_bps: Decimal = Decimal("15")
    max_open_trades: int = 3
    max_trade_notional_usd: Decimal = Decimal("200")
    daily_loss_cap_usd: Decimal = Decimal("5")
    max_trades_per_hour: int = 100
    cooldown_seconds: int = 5
    max_spread_bps: Decimal = Decimal("10")
    min_depth_usd: Decimal = Decimal("100")
    entry_slippage_bps: Decimal = Decimal("75")
    exit_slippage_bps: Decimal = Decimal("75")
    slippage_bps_min: Decimal = Decimal("5")
    slippage_bps_max: Decimal = Decimal("200")
    paper_mode: bool = True
    close_on_missing_position: bool = True
    close_fill_timeout_seconds: Decimal = Decimal("3.0")
    position_refresh_seconds: Decimal = Decimal("1.0")

    # === NEW: Maker mode settings ===
    order_mode: str = "taker"  # "taker" or "maker"
    symbols: list[str] = Field(
        default_factory=lambda: ["BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "AVAX", "SUI"],
        description="Whitelist of symbols for maker strategy (liquid markets only)",
    )
    maker_entry_timeout_s: float = 2.0
    maker_exit_timeout_s: float = 1.5
    maker_exit_max_retries: int = 3
    maker_reprice_delay_ms: int = 100
    max_hold_seconds: int = 60

    # === NEW: Risk guard settings ===
    hourly_loss_pause_usd: Decimal = Decimal("2.0")
    loss_streak_pause_count: int = 5
    min_fill_rate_percent: int = 20
    pause_duration_minutes: int = 30
```

**Step 4: Run test to verify it passes**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/config/test_surge_pro_maker_settings.py -v
```

Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add tests/unit/config/test_surge_pro_maker_settings.py src/funding_bot/config/settings.py
git commit -m "feat(surge-pro): add maker mode settings"
```

---

## Task 2: Create MakerExecutor Component

**Files:**
- Create: `src/funding_bot/services/surge_pro/maker_executor.py`
- Create: `tests/unit/services/surge_pro/test_maker_executor.py`

**Step 1: Write failing test for maker price calculation**

```python
"""Tests for MakerExecutor component."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from funding_bot.domain.models import Side
from funding_bot.services.surge_pro.maker_executor import MakerExecutor


class TestMakerPriceCalculation:
    """Test maker price calculation logic."""

    def test_buy_price_is_above_best_bid(self):
        """BUY maker price should be best_bid + 1 tick."""
        executor = MakerExecutor(settings=MagicMock(), x10=MagicMock())

        price = executor.calc_maker_price(
            side=Side.BUY,
            best_bid=Decimal("100.00"),
            best_ask=Decimal("100.10"),
            tick_size=Decimal("0.01"),
        )

        assert price == Decimal("100.01")

    def test_sell_price_is_below_best_ask(self):
        """SELL maker price should be best_ask - 1 tick."""
        executor = MakerExecutor(settings=MagicMock(), x10=MagicMock())

        price = executor.calc_maker_price(
            side=Side.SELL,
            best_bid=Decimal("100.00"),
            best_ask=Decimal("100.10"),
            tick_size=Decimal("0.01"),
        )

        assert price == Decimal("100.09")

    def test_price_respects_tick_size(self):
        """Price should be rounded to tick size."""
        executor = MakerExecutor(settings=MagicMock(), x10=MagicMock())

        # With larger tick size
        price = executor.calc_maker_price(
            side=Side.BUY,
            best_bid=Decimal("100.00"),
            best_ask=Decimal("101.00"),
            tick_size=Decimal("0.50"),
        )

        assert price == Decimal("100.50")
```

**Step 2: Run test to verify it fails**

```bash
mkdir -p tests/unit/services/surge_pro
touch tests/unit/services/surge_pro/__init__.py
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/test_maker_executor.py::TestMakerPriceCalculation -v
```

Expected: FAIL with ModuleNotFoundError

**Step 3: Implement MakerExecutor skeleton**

Create `src/funding_bot/services/surge_pro/maker_executor.py`:

```python
"""
Maker order executor for Surge Pro strategy.

Handles POST_ONLY order placement, fill monitoring, repricing, and taker fallback.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from funding_bot.domain.models import (
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Exchange,
)
from funding_bot.observability.logging import get_logger

if TYPE_CHECKING:
    from funding_bot.config.settings import SurgeProSettings

logger = get_logger(__name__)


@dataclass
class FillResult:
    """Result of a fill attempt."""

    filled: bool
    order: Order | None
    fill_price: Decimal | None
    fill_time_ms: float | None
    used_taker_fallback: bool = False


class MakerExecutor:
    """Executes maker orders with fill monitoring and taker fallback."""

    def __init__(self, *, settings: SurgeProSettings, x10):
        self.settings = settings
        self.x10 = x10

    def calc_maker_price(
        self,
        side: Side,
        best_bid: Decimal,
        best_ask: Decimal,
        tick_size: Decimal,
    ) -> Decimal:
        """Calculate aggressive maker price (1 tick better than best)."""
        if side == Side.BUY:
            # Place just above best bid for fill priority
            return best_bid + tick_size
        else:
            # Place just below best ask for fill priority
            return best_ask - tick_size

    async def place_maker_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        *,
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> Order:
        """Place a POST_ONLY maker order."""
        request = OrderRequest(
            symbol=symbol,
            exchange=Exchange.X10,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            price=price,
            time_in_force=TimeInForce.POST_ONLY,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
        )

        logger.info(
            "Placing maker order: symbol=%s side=%s qty=%s price=%s",
            symbol, side.value, qty, price,
        )

        return await self.x10.place_order(request)

    async def wait_for_fill(
        self,
        order: Order,
        timeout_s: float,
    ) -> FillResult:
        """Wait for order to fill within timeout."""
        start = time.monotonic()
        deadline = start + timeout_s

        while time.monotonic() < deadline:
            try:
                updated = await self.x10.get_order(order.symbol, order.order_id)
                if updated and updated.status == OrderStatus.FILLED:
                    fill_time = (time.monotonic() - start) * 1000
                    return FillResult(
                        filled=True,
                        order=updated,
                        fill_price=updated.avg_fill_price,
                        fill_time_ms=fill_time,
                    )
                if updated and updated.status.is_terminal():
                    # Cancelled, rejected, etc.
                    break
            except Exception as e:
                logger.debug("Fill check error: %s", e)

            await asyncio.sleep(0.1)

        return FillResult(filled=False, order=order, fill_price=None, fill_time_ms=None)

    async def cancel_order(self, order: Order) -> bool:
        """Cancel an unfilled order."""
        try:
            await self.x10.cancel_order(order.symbol, order.order_id)
            return True
        except Exception as e:
            logger.warning("Cancel failed: %s", e)
            return False

    async def place_taker_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        *,
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> Order:
        """Place an IOC taker order (fallback)."""
        request = OrderRequest(
            symbol=symbol,
            exchange=Exchange.X10,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            price=price,
            time_in_force=TimeInForce.IOC,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
        )

        logger.info(
            "Placing taker fallback: symbol=%s side=%s qty=%s price=%s",
            symbol, side.value, qty, price,
        )

        return await self.x10.place_order(request)
```

**Step 4: Run test to verify it passes**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/test_maker_executor.py::TestMakerPriceCalculation -v
```

Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/funding_bot/services/surge_pro/maker_executor.py tests/unit/services/surge_pro/
git commit -m "feat(surge-pro): add MakerExecutor with price calculation"
```

---

## Task 3: Add Fill Monitoring Tests

**Files:**
- Modify: `tests/unit/services/surge_pro/test_maker_executor.py`

**Step 1: Write failing test for fill monitoring**

Add to `tests/unit/services/surge_pro/test_maker_executor.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

from funding_bot.domain.models import Order, OrderStatus, Side, OrderType, TimeInForce, Exchange
from funding_bot.services.surge_pro.maker_executor import MakerExecutor, FillResult


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.maker_entry_timeout_s = 2.0
    settings.maker_exit_timeout_s = 1.5
    settings.maker_exit_max_retries = 3
    return settings


@pytest.fixture
def mock_x10():
    return AsyncMock()


@pytest.fixture
def sample_order():
    return Order(
        order_id="123",
        symbol="BTC",
        exchange=Exchange.X10,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("0.001"),
        price=Decimal("50000"),
        time_in_force=TimeInForce.POST_ONLY,
        status=OrderStatus.OPEN,
    )


class TestFillMonitoring:
    """Test fill monitoring and waiting logic."""

    @pytest.mark.asyncio
    async def test_returns_filled_when_order_fills(self, mock_settings, mock_x10, sample_order):
        """Should return filled=True when order fills."""
        filled_order = Order(
            order_id="123",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            time_in_force=TimeInForce.POST_ONLY,
            status=OrderStatus.FILLED,
            avg_fill_price=Decimal("50001"),
            filled_qty=Decimal("0.001"),
        )
        mock_x10.get_order = AsyncMock(return_value=filled_order)

        executor = MakerExecutor(settings=mock_settings, x10=mock_x10)
        result = await executor.wait_for_fill(sample_order, timeout_s=1.0)

        assert result.filled is True
        assert result.fill_price == Decimal("50001")
        assert result.fill_time_ms is not None

    @pytest.mark.asyncio
    async def test_returns_not_filled_on_timeout(self, mock_settings, mock_x10, sample_order):
        """Should return filled=False when timeout expires."""
        # Order stays OPEN
        mock_x10.get_order = AsyncMock(return_value=sample_order)

        executor = MakerExecutor(settings=mock_settings, x10=mock_x10)
        result = await executor.wait_for_fill(sample_order, timeout_s=0.3)

        assert result.filled is False
        assert result.fill_price is None

    @pytest.mark.asyncio
    async def test_returns_not_filled_when_cancelled(self, mock_settings, mock_x10, sample_order):
        """Should return filled=False when order is cancelled."""
        cancelled_order = Order(
            order_id="123",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            time_in_force=TimeInForce.POST_ONLY,
            status=OrderStatus.CANCELLED,
        )
        mock_x10.get_order = AsyncMock(return_value=cancelled_order)

        executor = MakerExecutor(settings=mock_settings, x10=mock_x10)
        result = await executor.wait_for_fill(sample_order, timeout_s=1.0)

        assert result.filled is False
```

**Step 2: Run tests**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/test_maker_executor.py::TestFillMonitoring -v
```

Expected: PASS (3 tests)

**Step 3: Commit**

```bash
git add tests/unit/services/surge_pro/test_maker_executor.py
git commit -m "test(surge-pro): add fill monitoring tests"
```

---

## Task 4: Add Entry Flow with Maker

**Files:**
- Create: `src/funding_bot/services/surge_pro/maker_engine.py`
- Create: `tests/unit/services/surge_pro/test_maker_engine.py`

**Step 1: Write failing test**

Create `tests/unit/services/surge_pro/test_maker_engine.py`:

```python
"""Tests for Surge Pro Maker Engine."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from funding_bot.domain.models import Side, Order, OrderStatus, Exchange, OrderType, TimeInForce
from funding_bot.services.surge_pro.maker_engine import SurgeProMakerEngine


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.surge_pro = MagicMock()
    settings.surge_pro.order_mode = "maker"
    settings.surge_pro.symbols = ["BTC", "ETH"]
    settings.surge_pro.entry_imbalance_threshold = Decimal("0.25")
    settings.surge_pro.max_entry_spread_bps = Decimal("10")
    settings.surge_pro.maker_entry_timeout_s = 2.0
    settings.surge_pro.maker_exit_timeout_s = 1.5
    settings.surge_pro.maker_exit_max_retries = 3
    settings.surge_pro.max_trade_notional_usd = Decimal("200")
    settings.surge_pro.max_open_trades = 3
    settings.surge_pro.daily_loss_cap_usd = Decimal("5")
    settings.surge_pro.paper_mode = False
    settings.live_trading = True
    return settings


@pytest.fixture
def mock_x10():
    x10 = AsyncMock()
    x10._markets = {
        "BTC-USD": MagicMock(
            base_asset="BTC",
            symbol="BTC-USD",
            tick_size=Decimal("1"),
            step_size=Decimal("0.00001"),
        ),
    }
    return x10


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestMakerEntry:
    """Test maker entry flow."""

    @pytest.mark.asyncio
    async def test_entry_uses_post_only_order(self, mock_settings, mock_x10, mock_store):
        """Entry should use POST_ONLY time-in-force."""
        # Setup orderbook
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={
            "best_bid": "50000",
            "best_ask": "50005",
            "bid_qty": "1.0",
            "ask_qty": "1.0",
        })
        mock_x10.get_orderbook_depth = AsyncMock(return_value={
            "bids": [(Decimal("50000"), Decimal("1.0"))],
            "asks": [(Decimal("50005"), Decimal("0.5"))],
        })

        # Capture placed order
        placed_order = None
        async def capture_order(request):
            nonlocal placed_order
            placed_order = request
            return Order(
                order_id="123",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_x10.list_positions = AsyncMock(return_value=[])
        mock_store.list_open_surge_trades = AsyncMock(return_value=[])
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
        mock_store.create_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        # Trigger entry
        await engine._maybe_enter_maker("BTC")

        # Verify POST_ONLY was used
        assert placed_order is not None
        assert placed_order.time_in_force == TimeInForce.POST_ONLY
```

**Step 2: Run test to verify it fails**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/test_maker_engine.py -v
```

Expected: FAIL with ModuleNotFoundError

**Step 3: Implement SurgeProMakerEngine**

Create `src/funding_bot/services/surge_pro/maker_engine.py`:

```python
"""
Surge Pro Maker Engine.

Volume farming strategy using POST_ONLY orders for 0% fees.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_FLOOR
from typing import TYPE_CHECKING

from funding_bot.config.settings import Settings
from funding_bot.domain.models import (
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SurgeTrade,
    SurgeTradeStatus,
    TimeInForce,
)
from funding_bot.observability.logging import LOG_TAG_PROFIT, get_logger
from funding_bot.services.surge_pro.signals import compute_imbalance, compute_spread_bps
from funding_bot.services.surge_pro.maker_executor import MakerExecutor

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)
STRATEGY_NAME = "surge_pro_maker"


class SurgeProMakerEngine:
    """Maker-based Surge Pro engine for volume farming."""

    def __init__(self, *, settings: Settings, x10, store, position_truth=None):
        self.settings = settings
        self.x10 = x10
        self.store = store
        self.position_truth = position_truth
        self.maker_executor = MakerExecutor(settings=settings.surge_pro, x10=x10)
        self._cooldowns: dict[str, float] = {}

    def _get_tick_size(self, symbol: str) -> Decimal:
        """Get tick size for symbol."""
        markets = getattr(self.x10, "_markets", {})
        market_name = f"{symbol}-USD" if not symbol.endswith("-USD") else symbol
        market = markets.get(market_name)
        if market:
            return getattr(market, "tick_size", Decimal("0.01"))
        return Decimal("0.01")

    def _get_step_size(self, symbol: str) -> Decimal:
        """Get step size (qty increment) for symbol."""
        markets = getattr(self.x10, "_markets", {})
        market_name = f"{symbol}-USD" if not symbol.endswith("-USD") else symbol
        market = markets.get(market_name)
        if market:
            return getattr(market, "step_size", Decimal("0.00001"))
        return Decimal("0.00001")

    async def _maybe_enter_maker(self, symbol: str) -> None:
        """Attempt maker entry for symbol."""
        # Get orderbook
        depth = await self.x10.get_orderbook_depth(symbol, 10)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []

        # Check imbalance
        imbalance = compute_imbalance(bids, asks, 10)
        threshold = self.settings.surge_pro.entry_imbalance_threshold

        side: Side | None = None
        if imbalance >= threshold:
            side = Side.BUY
        elif imbalance <= -threshold:
            side = Side.SELL

        if side is None:
            logger.debug("No entry signal for %s: imbalance=%s", symbol, imbalance)
            return

        # Get L1 for pricing
        l1 = await self.x10.get_orderbook_l1(symbol)
        best_bid = Decimal(str(l1.get("best_bid", "0")))
        best_ask = Decimal(str(l1.get("best_ask", "0")))

        # Check spread
        spread_bps = compute_spread_bps(best_bid, best_ask)
        if spread_bps > self.settings.surge_pro.max_spread_bps:
            logger.debug("Spread too wide for %s: %s bps", symbol, spread_bps)
            return

        # Calculate maker price
        tick_size = self._get_tick_size(symbol)
        maker_price = self.maker_executor.calc_maker_price(
            side=side,
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=tick_size,
        )

        # Calculate quantity
        notional = self.settings.surge_pro.max_trade_notional_usd
        mid = (best_bid + best_ask) / 2
        qty = notional / mid
        step_size = self._get_step_size(symbol)
        qty = (qty / step_size).to_integral_value(rounding=ROUND_FLOOR) * step_size

        if qty <= 0:
            return

        # Place POST_ONLY order
        client_order_id = f"surge_maker_entry_{uuid.uuid4().hex[:8]}"

        order = await self.x10.place_order(OrderRequest(
            symbol=symbol,
            exchange=Exchange.X10,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            price=maker_price,
            time_in_force=TimeInForce.POST_ONLY,
            client_order_id=client_order_id,
        ))

        # Wait for fill
        timeout = self.settings.surge_pro.maker_entry_timeout_s
        result = await self.maker_executor.wait_for_fill(order, timeout)

        if result.filled:
            # Create trade record
            trade = SurgeTrade(
                trade_id=str(uuid.uuid4()),
                symbol=symbol,
                exchange=Exchange.X10,
                side=side,
                qty=result.order.filled_qty or qty,
                entry_price=result.fill_price or maker_price,
                status=SurgeTradeStatus.OPEN,
                opened_at=datetime.now(UTC),
                entry_order_id=order.order_id,
                entry_client_order_id=client_order_id,
            )
            await self.store.create_surge_trade(trade)
            logger.info(
                "Maker entry filled: symbol=%s side=%s price=%s fill_time=%sms",
                symbol, side.value, result.fill_price, result.fill_time_ms,
            )
        else:
            # Cancel unfilled order
            await self.maker_executor.cancel_order(order)
            logger.debug("Maker entry not filled, cancelled: symbol=%s", symbol)
```

**Step 4: Run test to verify it passes**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/test_maker_engine.py -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add src/funding_bot/services/surge_pro/maker_engine.py tests/unit/services/surge_pro/test_maker_engine.py
git commit -m "feat(surge-pro): add maker engine with POST_ONLY entry"
```

---

## Task 5: Add Exit Flow with Maker + Taker Fallback

**Files:**
- Modify: `src/funding_bot/services/surge_pro/maker_engine.py`
- Modify: `tests/unit/services/surge_pro/test_maker_engine.py`

**Step 1: Write failing test**

Add to `tests/unit/services/surge_pro/test_maker_engine.py`:

```python
class TestMakerExit:
    """Test maker exit flow with taker fallback."""

    @pytest.mark.asyncio
    async def test_exit_tries_maker_first(self, mock_settings, mock_x10, mock_store):
        """Exit should attempt POST_ONLY first."""
        # Setup
        mock_x10.get_orderbook_l1 = AsyncMock(return_value={
            "best_bid": "50000",
            "best_ask": "50005",
        })
        mock_x10.get_mark_price = AsyncMock(return_value=Decimal("50002"))

        placed_orders = []
        async def capture_order(request):
            placed_orders.append(request)
            return Order(
                order_id=f"order_{len(placed_orders)}",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_store.create_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        # Create open trade (LONG)
        trade = SurgeTrade(
            trade_id="test123",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.004"),
            entry_price=Decimal("50000"),
            status=SurgeTradeStatus.OPEN,
            opened_at=datetime.now(UTC),
        )

        # Exit
        await engine._close_trade_maker(trade, "SIGNAL_FLIP")

        # First order should be POST_ONLY
        assert len(placed_orders) >= 1
        assert placed_orders[0].time_in_force == TimeInForce.POST_ONLY
        assert placed_orders[0].side == Side.SELL  # Opposite of LONG

    @pytest.mark.asyncio
    async def test_exit_falls_back_to_taker(self, mock_settings, mock_x10, mock_store):
        """Exit should use IOC taker after maker retries exhausted."""
        mock_settings.surge_pro.maker_exit_max_retries = 2

        mock_x10.get_orderbook_l1 = AsyncMock(return_value={
            "best_bid": "50000",
            "best_ask": "50005",
        })
        mock_x10.get_mark_price = AsyncMock(return_value=Decimal("50002"))
        mock_x10.get_order = AsyncMock(return_value=Order(
            order_id="unfilled",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            qty=Decimal("0.004"),
            price=Decimal("50004"),
            time_in_force=TimeInForce.POST_ONLY,
            status=OrderStatus.OPEN,  # Never fills
        ))
        mock_x10.cancel_order = AsyncMock()

        placed_orders = []
        async def capture_order(request):
            placed_orders.append(request)
            return Order(
                order_id=f"order_{len(placed_orders)}",
                symbol="BTC",
                exchange=Exchange.X10,
                side=request.side,
                order_type=OrderType.LIMIT,
                qty=request.qty,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.FILLED if request.time_in_force == TimeInForce.IOC else OrderStatus.OPEN,
                avg_fill_price=request.price,
                filled_qty=request.qty,
            )

        mock_x10.place_order = capture_order
        mock_store.create_surge_trade = AsyncMock()

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        trade = SurgeTrade(
            trade_id="test123",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.004"),
            entry_price=Decimal("50000"),
            status=SurgeTradeStatus.OPEN,
            opened_at=datetime.now(UTC),
        )

        await engine._close_trade_maker(trade, "SIGNAL_FLIP")

        # Should have maker attempts + final taker
        taker_orders = [o for o in placed_orders if o.time_in_force == TimeInForce.IOC]
        assert len(taker_orders) >= 1, "Should fall back to taker"
```

**Step 2: Run test to verify it fails**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/test_maker_engine.py::TestMakerExit -v
```

Expected: FAIL (method not implemented)

**Step 3: Implement exit flow**

Add to `src/funding_bot/services/surge_pro/maker_engine.py`:

```python
    async def _close_trade_maker(self, trade: SurgeTrade, reason: str) -> None:
        """Close trade using maker orders with taker fallback."""
        exit_side = trade.side.inverse()

        # Get current prices
        l1 = await self.x10.get_orderbook_l1(trade.symbol)
        best_bid = Decimal(str(l1.get("best_bid", "0")))
        best_ask = Decimal(str(l1.get("best_ask", "0")))
        tick_size = self._get_tick_size(trade.symbol)

        max_retries = self.settings.surge_pro.maker_exit_max_retries
        timeout = self.settings.surge_pro.maker_exit_timeout_s

        for attempt in range(max_retries):
            # Refresh prices for repricing
            if attempt > 0:
                l1 = await self.x10.get_orderbook_l1(trade.symbol)
                best_bid = Decimal(str(l1.get("best_bid", "0")))
                best_ask = Decimal(str(l1.get("best_ask", "0")))

            maker_price = self.maker_executor.calc_maker_price(
                side=exit_side,
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=tick_size,
            )

            client_order_id = f"surge_maker_exit_{uuid.uuid4().hex[:8]}"

            order = await self.x10.place_order(OrderRequest(
                symbol=trade.symbol,
                exchange=Exchange.X10,
                side=exit_side,
                qty=trade.qty,
                order_type=OrderType.LIMIT,
                price=maker_price,
                time_in_force=TimeInForce.POST_ONLY,
                reduce_only=True,
                client_order_id=client_order_id,
            ))

            result = await self.maker_executor.wait_for_fill(order, timeout)

            if result.filled:
                await self._finalize_close(trade, reason, result.fill_price, result.order)
                logger.info(
                    "Maker exit filled: symbol=%s reason=%s attempt=%d",
                    trade.symbol, reason, attempt + 1,
                )
                return

            # Cancel and retry
            await self.maker_executor.cancel_order(order)
            logger.debug("Maker exit attempt %d failed, retrying", attempt + 1)

        # Taker fallback
        logger.warning("Maker exit exhausted, using taker fallback: symbol=%s", trade.symbol)
        await self._close_trade_taker(trade, reason)

    async def _close_trade_taker(self, trade: SurgeTrade, reason: str) -> None:
        """Close trade using IOC taker order (fallback)."""
        exit_side = trade.side.inverse()

        l1 = await self.x10.get_orderbook_l1(trade.symbol)
        best_bid = Decimal(str(l1.get("best_bid", "0")))
        best_ask = Decimal(str(l1.get("best_ask", "0")))

        # Aggressive taker price
        if exit_side == Side.BUY:
            price = best_ask * Decimal("1.005")  # 0.5% slippage
        else:
            price = best_bid * Decimal("0.995")

        client_order_id = f"surge_taker_exit_{uuid.uuid4().hex[:8]}"

        order = await self.x10.place_order(OrderRequest(
            symbol=trade.symbol,
            exchange=Exchange.X10,
            side=exit_side,
            qty=trade.qty,
            order_type=OrderType.LIMIT,
            price=price,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            client_order_id=client_order_id,
        ))

        if order.status == OrderStatus.FILLED:
            await self._finalize_close(trade, reason, order.avg_fill_price, order)
        else:
            logger.error("Taker exit failed: symbol=%s status=%s", trade.symbol, order.status)

    async def _finalize_close(
        self,
        trade: SurgeTrade,
        reason: str,
        exit_price: Decimal | None,
        order: Order | None,
    ) -> None:
        """Finalize trade closure."""
        price = exit_price or trade.entry_price
        fees = trade.fees + (order.fee if order and order.fee else Decimal("0"))

        closed_trade = replace(
            trade,
            exit_price=price,
            fees=fees,
            status=SurgeTradeStatus.CLOSED,
            exit_reason=reason,
            closed_at=datetime.now(UTC),
            exit_order_id=order.order_id if order else None,
        )

        await self.store.create_surge_trade(closed_trade)

        # Log PnL
        if trade.side == Side.BUY:
            pnl = (price - trade.entry_price) * trade.qty
        else:
            pnl = (trade.entry_price - price) * trade.qty

        net_pnl = pnl - fees

        logger.info(
            "%s Surge Pro close: symbol=%s reason=%s pnl=%s net=%s fees=%s",
            LOG_TAG_PROFIT,
            trade.symbol,
            reason,
            pnl,
            net_pnl,
            fees,
        )
```

**Step 4: Run test to verify it passes**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/test_maker_engine.py::TestMakerExit -v
```

Expected: PASS (2 tests)

**Step 5: Commit**

```bash
git add src/funding_bot/services/surge_pro/maker_engine.py tests/unit/services/surge_pro/test_maker_engine.py
git commit -m "feat(surge-pro): add maker exit with taker fallback"
```

---

## Task 6: Add RiskGuard Component

**Files:**
- Create: `src/funding_bot/services/surge_pro/risk_guard.py`
- Create: `tests/unit/services/surge_pro/test_risk_guard.py`

**Step 1: Write failing test**

Create `tests/unit/services/surge_pro/test_risk_guard.py`:

```python
"""Tests for RiskGuard component."""

import pytest
from decimal import Decimal
from datetime import datetime, UTC, timedelta
from unittest.mock import AsyncMock, MagicMock

from funding_bot.services.surge_pro.risk_guard import RiskGuard


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.daily_loss_cap_usd = Decimal("5")
    settings.hourly_loss_pause_usd = Decimal("2")
    settings.loss_streak_pause_count = 5
    settings.min_fill_rate_percent = 20
    settings.pause_duration_minutes = 30
    return settings


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestDailyLossCap:
    """Test daily loss cap detection."""

    @pytest.mark.asyncio
    async def test_blocks_when_daily_cap_reached(self, mock_settings, mock_store):
        """Should block trading when daily loss cap reached."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-5.50"))

        guard = RiskGuard(settings=mock_settings, store=mock_store)
        result = await guard.check()

        assert result.blocked is True
        assert result.reason == "daily_loss_cap"

    @pytest.mark.asyncio
    async def test_allows_when_under_cap(self, mock_settings, mock_store):
        """Should allow trading when under daily loss cap."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-2.00"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-0.50"))
        mock_store.get_recent_trades = AsyncMock(return_value=[])
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        guard = RiskGuard(settings=mock_settings, store=mock_store)
        result = await guard.check()

        assert result.blocked is False


class TestLossStreak:
    """Test loss streak detection."""

    @pytest.mark.asyncio
    async def test_pauses_on_loss_streak(self, mock_settings, mock_store):
        """Should pause on consecutive losses."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-1.00"))
        mock_store.get_hourly_pnl = AsyncMock(return_value=Decimal("-0.50"))

        # 5 consecutive losses
        losses = [MagicMock(pnl=Decimal("-0.10")) for _ in range(5)]
        mock_store.get_recent_trades = AsyncMock(return_value=losses)
        mock_store.get_hourly_fill_rate = AsyncMock(return_value=0.5)

        guard = RiskGuard(settings=mock_settings, store=mock_store)
        result = await guard.check()

        assert result.blocked is True
        assert result.reason == "loss_streak"
        assert result.pause_minutes == 30
```

**Step 2: Run test to verify it fails**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/test_risk_guard.py -v
```

Expected: FAIL with ModuleNotFoundError

**Step 3: Implement RiskGuard**

Create `src/funding_bot/services/surge_pro/risk_guard.py`:

```python
"""
Risk Guard for Surge Pro strategy.

Monitors trading activity and pauses on anomalies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from funding_bot.observability.logging import get_logger

if TYPE_CHECKING:
    from funding_bot.config.settings import SurgeProSettings

logger = get_logger(__name__)


@dataclass
class RiskCheckResult:
    """Result of a risk check."""

    blocked: bool
    reason: str | None = None
    pause_minutes: int | None = None
    resume_at: datetime | None = None


class RiskGuard:
    """Monitors and guards against excessive risk."""

    def __init__(self, *, settings: SurgeProSettings, store):
        self.settings = settings
        self.store = store
        self._paused_until: datetime | None = None
        self._pause_count: int = 0

    async def check(self) -> RiskCheckResult:
        """Check all risk conditions."""
        now = datetime.now(UTC)

        # Check if currently paused
        if self._paused_until and now < self._paused_until:
            remaining = (self._paused_until - now).total_seconds() / 60
            return RiskCheckResult(
                blocked=True,
                reason="paused",
                pause_minutes=int(remaining),
                resume_at=self._paused_until,
            )

        # Clear pause if expired
        if self._paused_until and now >= self._paused_until:
            self._paused_until = None

        # Check daily loss cap (hard limit)
        daily_pnl = await self.store.get_daily_pnl()
        if daily_pnl <= -self.settings.daily_loss_cap_usd:
            return RiskCheckResult(
                blocked=True,
                reason="daily_loss_cap",
            )

        # Check hourly loss (soft limit - pause)
        hourly_pnl = await self.store.get_hourly_pnl()
        if hourly_pnl <= -self.settings.hourly_loss_pause_usd:
            self._trigger_pause("hourly_loss")
            return RiskCheckResult(
                blocked=True,
                reason="hourly_loss",
                pause_minutes=self.settings.pause_duration_minutes,
                resume_at=self._paused_until,
            )

        # Check loss streak
        recent_trades = await self.store.get_recent_trades(
            count=self.settings.loss_streak_pause_count
        )
        if len(recent_trades) >= self.settings.loss_streak_pause_count:
            all_losses = all(t.pnl < 0 for t in recent_trades)
            if all_losses:
                self._trigger_pause("loss_streak")
                return RiskCheckResult(
                    blocked=True,
                    reason="loss_streak",
                    pause_minutes=self.settings.pause_duration_minutes,
                    resume_at=self._paused_until,
                )

        # Check fill rate
        fill_rate = await self.store.get_hourly_fill_rate()
        min_rate = self.settings.min_fill_rate_percent / 100
        if fill_rate < min_rate and fill_rate > 0:  # 0 means no data
            self._trigger_pause("low_fill_rate")
            return RiskCheckResult(
                blocked=True,
                reason="low_fill_rate",
                pause_minutes=self.settings.pause_duration_minutes // 2,
                resume_at=self._paused_until,
            )

        return RiskCheckResult(blocked=False)

    def _trigger_pause(self, reason: str) -> None:
        """Trigger a pause."""
        duration = self.settings.pause_duration_minutes
        self._paused_until = datetime.now(UTC) + timedelta(minutes=duration)
        self._pause_count += 1

        logger.warning(
            "RiskGuard pause triggered: reason=%s duration=%dm count=%d",
            reason, duration, self._pause_count,
        )

    def reset(self) -> None:
        """Reset pause state (for testing/manual intervention)."""
        self._paused_until = None
        self._pause_count = 0
```

**Step 4: Run test to verify it passes**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/test_risk_guard.py -v
```

Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/funding_bot/services/surge_pro/risk_guard.py tests/unit/services/surge_pro/test_risk_guard.py
git commit -m "feat(surge-pro): add RiskGuard for anomaly detection"
```

---

## Task 7: Wire Up Main Loop

**Files:**
- Modify: `src/funding_bot/services/surge_pro/maker_engine.py`
- Add integration with supervisor

**Step 1: Write integration test**

Add to `tests/unit/services/surge_pro/test_maker_engine.py`:

```python
class TestMainLoop:
    """Test main tick loop."""

    @pytest.mark.asyncio
    async def test_tick_checks_risk_guard_first(self, mock_settings, mock_x10, mock_store):
        """Main tick should check RiskGuard before entering."""
        mock_store.get_daily_pnl = AsyncMock(return_value=Decimal("-6.00"))  # Over cap
        mock_store.list_open_surge_trades = AsyncMock(return_value=[])
        mock_x10.list_positions = AsyncMock(return_value=[])

        engine = SurgeProMakerEngine(
            settings=mock_settings,
            x10=mock_x10,
            store=mock_store,
        )

        # Should not place any orders when risk blocked
        mock_x10.place_order = AsyncMock()

        await engine.tick()

        mock_x10.place_order.assert_not_called()
```

**Step 2: Implement tick loop**

Add to `src/funding_bot/services/surge_pro/maker_engine.py`:

```python
    async def tick(self) -> None:
        """Main loop tick - scan for entries and manage exits."""
        # Check risk limits
        risk_result = await self.risk_guard.check()
        if risk_result.blocked:
            logger.debug("Tick blocked by RiskGuard: %s", risk_result.reason)
            return

        # Get current positions
        positions = await self.x10.list_positions()
        position_symbols = {p.symbol.replace("-USD", "") for p in positions if p.qty > 0}

        # Check open trades for exits
        open_trades = await self.store.list_open_surge_trades()
        for trade in open_trades:
            await self._maybe_exit(trade)

        # Check for new entries
        if len(positions) >= self.settings.surge_pro.max_open_trades:
            return

        open_symbols = {t.symbol for t in open_trades}
        for symbol in self.settings.surge_pro.symbols:
            if symbol in open_symbols:
                continue
            if self._is_on_cooldown(symbol):
                continue
            await self._maybe_enter_maker(symbol)

    async def _maybe_exit(self, trade: SurgeTrade) -> None:
        """Check if trade should be exited."""
        if trade.status != SurgeTradeStatus.OPEN:
            return

        # Get current price
        try:
            mark_price = await self.x10.get_mark_price(trade.symbol)
        except Exception:
            l1 = await self.x10.get_orderbook_l1(trade.symbol)
            bid = Decimal(str(l1.get("best_bid", "0")))
            ask = Decimal(str(l1.get("best_ask", "0")))
            mark_price = (bid + ask) / 2

        if mark_price <= 0:
            return

        # Calculate PnL in bps
        if trade.side == Side.BUY:
            pnl_bps = ((mark_price - trade.entry_price) / trade.entry_price) * 10000
        else:
            pnl_bps = ((trade.entry_price - mark_price) / trade.entry_price) * 10000

        # Check stop loss
        if pnl_bps <= -self.settings.surge_pro.stop_loss_bps:
            await self._close_trade_taker(trade, "STOP_LOSS")  # Immediate taker for SL
            return

        # Check take profit
        if pnl_bps >= self.settings.surge_pro.take_profit_bps:
            await self._close_trade_maker(trade, "TAKE_PROFIT")
            return

        # Check signal flip
        depth = await self.x10.get_orderbook_depth(trade.symbol, 10)
        imbalance = compute_imbalance(depth.get("bids", []), depth.get("asks", []), 10)
        exit_threshold = self.settings.surge_pro.exit_imbalance_threshold

        if trade.side == Side.BUY and imbalance <= -exit_threshold:
            await self._close_trade_maker(trade, "SIGNAL_FLIP")
        elif trade.side == Side.SELL and imbalance >= exit_threshold:
            await self._close_trade_maker(trade, "SIGNAL_FLIP")

    def _is_on_cooldown(self, symbol: str) -> bool:
        """Check if symbol is on cooldown."""
        import time
        cooldown_until = self._cooldowns.get(symbol)
        if not cooldown_until:
            return False
        if time.time() >= cooldown_until:
            del self._cooldowns[symbol]
            return False
        return True

    def _set_cooldown(self, symbol: str) -> None:
        """Set cooldown for symbol."""
        import time
        self._cooldowns[symbol] = time.time() + self.settings.surge_pro.cooldown_seconds
```

Also add RiskGuard initialization in `__init__`:

```python
from funding_bot.services.surge_pro.risk_guard import RiskGuard

# In __init__:
self.risk_guard = RiskGuard(settings=settings.surge_pro, store=store)
```

**Step 3: Run tests**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/services/surge_pro/ -v
```

Expected: All PASS

**Step 4: Commit**

```bash
git add src/funding_bot/services/surge_pro/
git commit -m "feat(surge-pro): wire up main tick loop with risk guard"
```

---

## Task 8: Update Exports and Integration

**Files:**
- Modify: `src/funding_bot/services/surge_pro/__init__.py`
- Modify: `src/funding_bot/app/supervisor/loops.py` (to use new engine when maker mode)

**Step 1: Update exports**

```python
# src/funding_bot/services/surge_pro/__init__.py
"""Surge Pro strategy components."""

from funding_bot.services.surge_pro.engine import SurgeProEngine
from funding_bot.services.surge_pro.maker_engine import SurgeProMakerEngine
from funding_bot.services.surge_pro.maker_executor import MakerExecutor
from funding_bot.services.surge_pro.risk_guard import RiskGuard
from funding_bot.services.surge_pro.signals import compute_imbalance, compute_spread_bps

__all__ = [
    "SurgeProEngine",
    "SurgeProMakerEngine",
    "MakerExecutor",
    "RiskGuard",
    "compute_imbalance",
    "compute_spread_bps",
]
```

**Step 2: Commit**

```bash
git add src/funding_bot/services/surge_pro/__init__.py
git commit -m "feat(surge-pro): export maker engine components"
```

---

## Task 9: Add Store Methods for RiskGuard

**Files:**
- Modify: `src/funding_bot/adapters/store/sqlite/store.py`
- Add: `get_daily_pnl`, `get_hourly_pnl`, `get_recent_trades`, `get_hourly_fill_rate`

**Step 1: Write failing tests**

Create `tests/unit/adapters/store/sqlite/test_surge_risk_queries.py`:

```python
"""Tests for surge pro risk-related store queries."""

import pytest
from decimal import Decimal
from datetime import datetime, UTC, timedelta

from funding_bot.domain.models import SurgeTrade, SurgeTradeStatus, Side, Exchange


@pytest.mark.asyncio
async def test_get_daily_pnl(sqlite_store):
    """Should calculate daily PnL from closed trades."""
    now = datetime.now(UTC)

    # Create closed trades today
    for i, pnl in enumerate([Decimal("0.10"), Decimal("-0.15"), Decimal("0.05")]):
        trade = SurgeTrade(
            trade_id=f"daily_{i}",
            symbol="BTC",
            exchange=Exchange.X10,
            side=Side.BUY,
            qty=Decimal("0.001"),
            entry_price=Decimal("50000"),
            exit_price=Decimal("50000") + (pnl / Decimal("0.001")),
            status=SurgeTradeStatus.CLOSED,
            opened_at=now - timedelta(hours=1),
            closed_at=now,
        )
        await sqlite_store.create_surge_trade(trade)

    daily_pnl = await sqlite_store.get_daily_pnl()
    assert daily_pnl == Decimal("0.00")  # 0.10 - 0.15 + 0.05 = 0.00


@pytest.mark.asyncio
async def test_get_hourly_pnl(sqlite_store):
    """Should calculate PnL from last hour."""
    # Implementation test
    pass


@pytest.mark.asyncio
async def test_get_recent_trades(sqlite_store):
    """Should return N most recent trades."""
    # Implementation test
    pass
```

**Step 2: Implement store methods**

Add to `src/funding_bot/adapters/store/sqlite/store.py`:

```python
async def get_daily_pnl(self) -> Decimal:
    """Get total PnL for today (UTC)."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    async with self._read_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN side = 'BUY'
                    THEN (exit_price - entry_price) * qty
                    ELSE (entry_price - exit_price) * qty
                END - COALESCE(fees, 0)
            ), 0) as pnl
            FROM surge_trades
            WHERE status = 'CLOSED'
            AND closed_at >= ?
            """,
            (today_start.isoformat(),),
        )
        row = await cursor.fetchone()
        return Decimal(str(row[0])) if row else Decimal("0")

async def get_hourly_pnl(self) -> Decimal:
    """Get total PnL for last hour."""
    hour_ago = datetime.now(UTC) - timedelta(hours=1)

    async with self._read_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN side = 'BUY'
                    THEN (exit_price - entry_price) * qty
                    ELSE (entry_price - exit_price) * qty
                END - COALESCE(fees, 0)
            ), 0) as pnl
            FROM surge_trades
            WHERE status = 'CLOSED'
            AND closed_at >= ?
            """,
            (hour_ago.isoformat(),),
        )
        row = await cursor.fetchone()
        return Decimal(str(row[0])) if row else Decimal("0")

async def get_recent_trades(self, count: int = 10) -> list[SurgeTrade]:
    """Get N most recent closed trades."""
    async with self._read_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM surge_trades
            WHERE status = 'CLOSED'
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (count,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_surge_trade(row) for row in rows]

async def get_hourly_fill_rate(self) -> float:
    """Get fill rate (filled / attempts) for last hour."""
    hour_ago = datetime.now(UTC) - timedelta(hours=1)

    async with self._read_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                COUNT(CASE WHEN status IN ('OPEN', 'CLOSED') THEN 1 END) as filled,
                COUNT(*) as total
            FROM surge_trades
            WHERE created_at >= ?
            """,
            (hour_ago.isoformat(),),
        )
        row = await cursor.fetchone()
        if row and row[1] > 0:
            return row[0] / row[1]
        return 0.0
```

**Step 3: Run tests and commit**

```bash
/home/koopm/funding-bot/.venv/bin/pytest tests/unit/adapters/store/sqlite/test_surge_risk_queries.py -v
git add src/funding_bot/adapters/store/sqlite/store.py tests/unit/adapters/store/sqlite/test_surge_risk_queries.py
git commit -m "feat(store): add risk guard query methods"
```

---

## Task 10: Final Integration Test

**Files:**
- Create: `tests/integration/surge_pro/test_maker_flow.py`

**Step 1: Write integration test**

```python
"""Integration test for Surge Pro maker flow."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from funding_bot.services.surge_pro.maker_engine import SurgeProMakerEngine
from funding_bot.domain.models import Order, OrderStatus, Exchange, Side, OrderType, TimeInForce


@pytest.mark.asyncio
async def test_full_maker_cycle():
    """Test complete entry -> exit cycle with maker orders."""
    # Setup mocks
    settings = MagicMock()
    settings.surge_pro = MagicMock()
    settings.surge_pro.order_mode = "maker"
    settings.surge_pro.symbols = ["BTC"]
    settings.surge_pro.entry_imbalance_threshold = Decimal("0.25")
    settings.surge_pro.exit_imbalance_threshold = Decimal("0.10")
    settings.surge_pro.max_entry_spread_bps = Decimal("10")
    settings.surge_pro.maker_entry_timeout_s = 0.5
    settings.surge_pro.maker_exit_timeout_s = 0.5
    settings.surge_pro.maker_exit_max_retries = 2
    settings.surge_pro.max_trade_notional_usd = Decimal("200")
    settings.surge_pro.max_open_trades = 3
    settings.surge_pro.daily_loss_cap_usd = Decimal("5")
    settings.surge_pro.stop_loss_bps = Decimal("20")
    settings.surge_pro.take_profit_bps = Decimal("15")
    settings.surge_pro.cooldown_seconds = 1
    settings.surge_pro.hourly_loss_pause_usd = Decimal("2")
    settings.surge_pro.loss_streak_pause_count = 5
    settings.surge_pro.min_fill_rate_percent = 20
    settings.surge_pro.pause_duration_minutes = 30
    settings.live_trading = True

    x10 = AsyncMock()
    x10._markets = {
        "BTC-USD": MagicMock(
            tick_size=Decimal("1"),
            step_size=Decimal("0.00001"),
        ),
    }

    store = AsyncMock()
    store.get_daily_pnl = AsyncMock(return_value=Decimal("0"))
    store.get_hourly_pnl = AsyncMock(return_value=Decimal("0"))
    store.get_recent_trades = AsyncMock(return_value=[])
    store.get_hourly_fill_rate = AsyncMock(return_value=0.5)
    store.list_open_surge_trades = AsyncMock(return_value=[])
    store.create_surge_trade = AsyncMock()

    # Entry orderbook - strong buy signal
    x10.get_orderbook_depth = AsyncMock(return_value={
        "bids": [(Decimal("50000"), Decimal("10.0"))],  # Heavy bids
        "asks": [(Decimal("50005"), Decimal("1.0"))],   # Light asks
    })
    x10.get_orderbook_l1 = AsyncMock(return_value={
        "best_bid": "50000",
        "best_ask": "50005",
    })
    x10.list_positions = AsyncMock(return_value=[])

    # Orders fill immediately
    x10.place_order = AsyncMock(return_value=Order(
        order_id="123",
        symbol="BTC",
        exchange=Exchange.X10,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("0.004"),
        price=Decimal("50001"),
        time_in_force=TimeInForce.POST_ONLY,
        status=OrderStatus.FILLED,
        avg_fill_price=Decimal("50001"),
        filled_qty=Decimal("0.004"),
    ))
    x10.get_order = AsyncMock(return_value=Order(
        order_id="123",
        symbol="BTC",
        exchange=Exchange.X10,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("0.004"),
        price=Decimal("50001"),
        time_in_force=TimeInForce.POST_ONLY,
        status=OrderStatus.FILLED,
        avg_fill_price=Decimal("50001"),
        filled_qty=Decimal("0.004"),
    ))

    engine = SurgeProMakerEngine(settings=settings, x10=x10, store=store)

    # Run tick - should enter
    await engine.tick()

    # Verify POST_ONLY was used
    call_args = x10.place_order.call_args
    assert call_args is not None
    request = call_args[0][0]
    assert request.time_in_force == TimeInForce.POST_ONLY

    # Verify trade was created
    store.create_surge_trade.assert_called()
```

**Step 2: Run test**

```bash
mkdir -p tests/integration/surge_pro
touch tests/integration/surge_pro/__init__.py
/home/koopm/funding-bot/.venv/bin/pytest tests/integration/surge_pro/test_maker_flow.py -v
```

**Step 3: Commit**

```bash
git add tests/integration/surge_pro/
git commit -m "test(surge-pro): add maker flow integration test"
```

---

## Summary

After completing all tasks:

1. **Settings** - New maker mode config options
2. **MakerExecutor** - Core maker order logic
3. **MakerEngine** - Main engine using POST_ONLY orders
4. **RiskGuard** - 24/7 anomaly detection
5. **Store methods** - Risk metrics queries
6. **Integration** - Full cycle test

**Total estimated commits:** 10

**Next steps after implementation:**
- Run full test suite
- Test in paper mode
- Deploy to live with small position sizes
- Monitor fill rates and adjust timeouts

---

*Plan created: 2026-01-19*
