import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)


class ShutdownResult(Dict[str, Any]):
    """Typed-ish result object for shutdown attempts."""
    pass


class ShutdownOrchestrator:
    """
    Centralized, idempotent shutdown coordinator.

    Sequence:
    1) Set shutdown flag (block new orders)
    2) Cancel open orders (targeted)
    3) Close positions (reduce-only, IOC, escalating slippage)
    4) Verify & retry (re-fetch positions)
    5) Persist (state/db/fee-manager)
    6) Teardown (WS, parallel exec, adapters, telegram)
    """

    def __init__(
        self,
        global_timeout: float = 120.0,
        verify_retries: int = 5,
        verify_delay: float = 2.0,
        base_slippage: float = 0.02,
    ):
        self.global_timeout = global_timeout
        self.verify_retries = verify_retries
        self.verify_delay = verify_delay
        self.base_slippage = base_slippage

        self._components: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._in_progress = False
        self._last_result: Optional[ShutdownResult] = None

    def configure(self, **components: Any) -> None:
        """Register/override components lazily; ignore None."""
        for name, comp in components.items():
            if comp is not None:
                self._components[name] = comp

    async def shutdown(self, reason: str = "") -> ShutdownResult:
        """Run the orchestrated shutdown (idempotent)."""
        async with self._lock:
            if self._in_progress:
                return self._last_result or {
                    "success": False,
                    "errors": ["shutdown_already_running"],
                    "remaining": {},
                    "elapsed": 0.0,
                    "reason": reason,
                }
            self._in_progress = True

        start = time.monotonic()
        errors: List[str] = []
        remaining: Dict[str, Any] = {}

        try:
            # Block new trading immediately
            try:
                config.IS_SHUTTING_DOWN = True
            except Exception:
                pass

            logger.info(f"🛑 Shutdown orchestrator start (reason={reason})")

            async with asyncio.timeout(self.global_timeout):
                await self._cancel_open_orders(errors)
                await self._close_and_verify_positions(errors)
                # Extra safety: re-cancel after close attempts to catch makers
                await self._cancel_open_orders(errors)
                await self._persist_state(errors)
                await self._teardown_components(errors)

        except asyncio.TimeoutError:
            errors.append("shutdown_timeout")
            logger.error("❌ Shutdown exceeded global timeout")
        except Exception as e:  # noqa: BLE001
            msg = f"shutdown_unexpected_error:{e}"
            errors.append(msg)
            logger.error(f"❌ Unexpected shutdown error: {e}", exc_info=True)
        finally:
            elapsed = time.monotonic() - start
            remaining = await self._fetch_positions_safely()

            # If no positions remain, drop close-timeout warning
            if not remaining.get("lighter") and not remaining.get("x10"):
                errors = [e for e in errors if e != "close_positions_timeout"]

            success = (
                not errors and
                not remaining.get("lighter") and
                not remaining.get("x10")
            )
            result: ShutdownResult = {
                "success": success,
                "errors": errors,
                "remaining": remaining,
                "elapsed": elapsed,
                "reason": reason,
            }
            self._last_result = result
            self._in_progress = False
            logger.info(
                "✅ Shutdown orchestrator finished "
                f"(success={success}, remaining={len(remaining.get('x10', [])) + len(remaining.get('lighter', []))}, "
                f"errors={errors}, elapsed={elapsed:.2f}s)"
            )
            return result

    # ----- Phases -----------------------------------------------------
    async def _cancel_open_orders(self, errors: List[str]) -> None:
        """Cancel open orders for relevant symbols with timeout."""
        lighter = self._components.get("lighter")
        x10 = self._components.get("x10")

        symbols = await self._collect_relevant_symbols()
        if not symbols:
            logger.info("🔕 No relevant symbols found for cancellation")
            return

        tasks = []
        for sym in symbols:
            if lighter and hasattr(lighter, "cancel_all_orders"):
                tasks.append(lighter.cancel_all_orders(sym))
            if x10 and hasattr(x10, "cancel_all_orders"):
                tasks.append(x10.cancel_all_orders(sym))

        if not tasks:
            return

        logger.info(f"🛑 Cancelling open orders for {len(symbols)} symbols...")
        try:
            async with asyncio.timeout(10.0):
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.TimeoutError:
            errors.append("cancel_orders_timeout")
            logger.warning("⚠️ Cancel orders timed out")

    async def _close_and_verify_positions(self, errors: List[str]) -> None:
        """Close positions with retries and verification."""
        lighter = self._components.get("lighter")
        x10 = self._components.get("x10")

        if not lighter and not x10:
            logger.info("No adapters registered; skipping close phase")
            return

        for attempt in range(self.verify_retries):
            positions = await self._fetch_positions()
            if not positions["x10"] and not positions["lighter"]:
                if attempt == 0:
                    logger.info("✅ No open positions to close")
                else:
                    logger.info("✅ No open positions remaining")
                return

            logger.info(
                f"🔻 Close attempt {attempt + 1}/{self.verify_retries} | "
                f"x10={len(positions['x10'])}, lighter={len(positions['lighter'])}"
            )

            # Close X10 positions
            x10_tasks = []
            for pos in positions["x10"]:
                size = self._safe_float(pos.get("size", 0))
                if size == 0:
                    continue
                symbol = pos.get("symbol")
                side = "BUY" if size < 0 else "SELL" if size > 0 else None
                if not symbol or not side:
                    continue
                try:
                    logger.info(f"🛑 Closing X10 {symbol} ({side} {abs(size)})")
                    x10_tasks.append(x10.close_live_position(symbol, side, abs(size)))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"x10_close_prepare:{symbol}:{e}")

            # Close Lighter positions with escalating slippage
            lighter_tasks = []
            for pos in positions["lighter"]:
                size = self._safe_float(pos.get("size", 0))
                if size == 0:
                    continue
                symbol = pos.get("symbol")
                if not symbol:
                    continue
                close_side = "SELL" if size > 0 else "BUY"
                price = None
                try:
                    price = lighter.get_price(symbol)
                except Exception:
                    price = None

                # Escalating but capped slippage; final attempt uses market (agg_price=None)
                slip = min(self.base_slippage + (0.02 * attempt), 0.08)
                agg_price = None
                if attempt < self.verify_retries - 1 and price:
                    agg_price = price * (1 + slip) if close_side == "BUY" else price * (1 - slip)

                try:
                    logger.info(
                        f"🛑 Closing Lighter {symbol}: {close_side} {abs(size)} "
                        f"(IOC reduce_only, slip~{slip:.0%}, agg_price={agg_price})"
                    )
                    lighter_tasks.append(
                        lighter.open_live_position(
                            symbol=symbol,
                            side=close_side,
                            notional_usd=0,
                            amount=abs(size),
                            price=agg_price,
                            post_only=False,
                            reduce_only=True,
                            time_in_force="IOC",
                        )
                    )
                except Exception as e:  # noqa: BLE001
                    errors.append(f"lighter_close_prepare:{symbol}:{e}")

            close_tasks = []
            if x10_tasks:
                close_tasks.extend(x10_tasks)
            if lighter_tasks:
                close_tasks.extend(lighter_tasks)

            if close_tasks:
                try:
                    async with asyncio.timeout(25.0):
                        await asyncio.gather(*close_tasks, return_exceptions=True)
                except asyncio.TimeoutError:
                    errors.append("close_positions_timeout")
                    logger.warning("⚠️ Close positions timed out")

            if attempt < self.verify_retries - 1:
                await asyncio.sleep(self.verify_delay)

        # Final verification handled by caller via _fetch_positions_safely

    async def _persist_state(self, errors: List[str]) -> None:
        """Persist state and db."""
        state_manager = self._components.get("state_manager")
        close_database_fn = self._components.get("close_database_fn")
        stop_fee_manager_fn = self._components.get("stop_fee_manager_fn")

        if state_manager and hasattr(state_manager, "stop"):
            try:
                await asyncio.wait_for(state_manager.stop(), timeout=5.0)
            except Exception as e:  # noqa: BLE001
                errors.append(f"state_manager_stop:{e}")
                logger.error(f"State manager stop error: {e}")

        if stop_fee_manager_fn:
            try:
                await asyncio.wait_for(stop_fee_manager_fn(), timeout=3.0)
            except Exception as e:  # noqa: BLE001
                errors.append(f"fee_manager_stop:{e}")

        if close_database_fn:
            try:
                await asyncio.wait_for(close_database_fn(), timeout=3.0)
            except Exception as e:  # noqa: BLE001
                errors.append(f"close_database:{e}")

        logger.info("💾 Persist phase complete")

    async def _teardown_components(self, errors: List[str]) -> None:
        """Stop WS, execution, telegram, adapters."""
        ws_manager = self._components.get("ws_manager")
        parallel_exec = self._components.get("parallel_exec")
        telegram_bot = self._components.get("telegram_bot")
        lighter = self._components.get("lighter")
        x10 = self._components.get("x10")

        async def _safe_call(coro_factory, label: str, timeout: float = 5.0) -> None:
            try:
                coro = coro_factory()
                await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                errors.append(f"{label}_timeout")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{label}_error:{e}")

        if ws_manager and hasattr(ws_manager, "stop"):
            await _safe_call(ws_manager.stop, "ws_stop", timeout=5.0)

        if parallel_exec and hasattr(parallel_exec, "stop"):
            await _safe_call(parallel_exec.stop, "parallel_exec_stop", timeout=5.0)

        if telegram_bot and hasattr(telegram_bot, "stop"):
            try:
                await telegram_bot.send_message("🛑 Bot shutting down")
            except Exception:
                pass
            await _safe_call(telegram_bot.stop, "telegram_stop", timeout=3.0)

        if lighter and hasattr(lighter, "aclose"):
            await _safe_call(lighter.aclose, "lighter_aclose", timeout=3.0)

        if x10 and hasattr(x10, "aclose"):
            await _safe_call(x10.aclose, "x10_aclose", timeout=3.0)

        logger.info("✅ Teardown complete")

    # ----- Helpers ----------------------------------------------------
    async def _collect_relevant_symbols(self) -> List[str]:
        """Gather symbols from state, active executions, and current positions."""
        symbols: set[str] = set()
        state_manager = self._components.get("state_manager")
        lighter = self._components.get("lighter")
        x10 = self._components.get("x10")
        parallel_exec = self._components.get("parallel_exec")

        if state_manager and hasattr(state_manager, "get_all_open_trades"):
            try:
                trades = await state_manager.get_all_open_trades()
                for t in trades or []:
                    if getattr(t, "symbol", None):
                        symbols.add(t.symbol)
            except Exception:
                pass

        # Active executions may hold maker orders without positions yet
        if parallel_exec and getattr(parallel_exec, "active_executions", None):
            try:
                symbols.update(parallel_exec.active_executions.keys())
            except Exception:
                pass

        positions = await self._fetch_positions()
        for pos in positions["lighter"]:
            sym = pos.get("symbol")
            if sym:
                symbols.add(sym)
        for pos in positions["x10"]:
            sym = pos.get("symbol")
            if sym:
                symbols.add(sym)

        # Fallback to market list if empty
        if not symbols:
            try:
                if lighter and getattr(lighter, "market_info", None):
                    symbols.update(lighter.market_info.keys())
                if x10 and getattr(x10, "market_info", None):
                    symbols.update(x10.market_info.keys())
            except Exception:
                pass

        return list(symbols)

    async def _fetch_positions(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch positions from both exchanges with safety."""
        lighter = self._components.get("lighter")
        x10 = self._components.get("x10")
        positions = {"lighter": [], "x10": []}

        if lighter and hasattr(lighter, "fetch_open_positions"):
            try:
                async with asyncio.timeout(5.0):
                    positions["lighter"] = await lighter.fetch_open_positions() or []
            except Exception:
                positions["lighter"] = []

        if x10 and hasattr(x10, "fetch_open_positions"):
            try:
                async with asyncio.timeout(5.0):
                    positions["x10"] = await x10.fetch_open_positions() or []
            except Exception:
                positions["x10"] = []

        return positions

    async def _fetch_positions_safely(self) -> Dict[str, List[Dict[str, Any]]]:
        """Same as _fetch_positions but swallow all exceptions."""
        try:
            return await self._fetch_positions()
        except Exception:
            return {"lighter": [], "x10": []}

    @staticmethod
    def _safe_float(val: Any) -> float:
        try:
            return float(val)
        except Exception:
            return 0.0


_SHUTDOWN_SINGLETON: Optional[ShutdownOrchestrator] = None


def get_shutdown_orchestrator() -> ShutdownOrchestrator:
    global _SHUTDOWN_SINGLETON
    if _SHUTDOWN_SINGLETON is None:
        _SHUTDOWN_SINGLETON = ShutdownOrchestrator()
    return _SHUTDOWN_SINGLETON

