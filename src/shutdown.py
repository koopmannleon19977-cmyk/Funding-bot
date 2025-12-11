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
        global_timeout: float = 90.0,  # INCREASED: Need more time for graceful execution wait + position close
        verify_retries: int = 3,  # INCREASED: More retries for reliability
        verify_delay: float = 1.0,  # INCREASED: More delay for API rate limits
        base_slippage: float = 0.05,
    ):
        self.global_timeout = global_timeout
        self.verify_retries = verify_retries
        self.verify_delay = verify_delay
        self.base_slippage = base_slippage

        self._components: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._in_progress = False
        self._last_result: Optional[ShutdownResult] = None
        self._shutdown_started = False
        self._positions_at_start: Dict[str, List] = {}

    def configure(self, **components: Any) -> None:
        """Register/override components lazily; ignore None."""
        for name, comp in components.items():
            if comp is not None:
                self._components[name] = comp

    async def shutdown(self, reason: str = "") -> ShutdownResult:
        """Run the orchestrated shutdown with improved sequence."""
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
            self._shutdown_started = True

        start = time.monotonic()
        errors: List[str] = []
        remaining: Dict[str, Any] = {}

        try:
            # ═══════════════════════════════════════════════════════════
            # PHASE 0: Block new trades IMMEDIATELY
            # ═══════════════════════════════════════════════════════════
            try:
                config.IS_SHUTTING_DOWN = True
            except Exception:
                pass

            logger.info(f"🛑 Shutdown orchestrator start (reason={reason})")

            # ═══════════════════════════════════════════════════════════
            # PHASE 1: Snapshot current positions BEFORE any changes
            # ═══════════════════════════════════════════════════════════
            self._positions_at_start = await self._fetch_positions_safely()
            logger.info(
                f"📸 Position snapshot: X10={len(self._positions_at_start.get('x10', []))}, "
                f"Lighter={len(self._positions_at_start.get('lighter', []))}"
            )

            async with asyncio.timeout(self.global_timeout):
                # ═══════════════════════════════════════════════════════
                # PHASE 2: Wait for ParallelExecutionManager to finish
                # This is CRITICAL - let active trades complete or rollback
                # ═══════════════════════════════════════════════════════
                await self._wait_for_active_executions(errors)
                
                # ═══════════════════════════════════════════════════════
                # PHASE 3: Cancel any remaining open orders
                # ═══════════════════════════════════════════════════════
                await self._cancel_open_orders(errors)
                
                # ═══════════════════════════════════════════════════════
                # PHASE 4: Close positions with verification loop
                # ═══════════════════════════════════════════════════════
                await self._close_and_verify_positions(errors)
                
                # ═══════════════════════════════════════════════════════
                # PHASE 5: Final safety check - any orphaned positions?
                # ═══════════════════════════════════════════════════════
                await self._final_position_sweep(errors)
                
                # ═══════════════════════════════════════════════════════
                # PHASE 6: Persist state and cleanup
                # ═══════════════════════════════════════════════════════
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

            # Calculate success
            remaining_count = len(remaining.get("lighter", [])) + len(remaining.get("x10", []))
            success = remaining_count == 0 and "shutdown_timeout" not in errors
            
            result: ShutdownResult = {
                "success": success,
                "errors": errors,
                "remaining": remaining,
                "elapsed": elapsed,
                "reason": reason,
            }
            self._last_result = result
            self._in_progress = False
            
            if success:
                logger.info(f"✅ All positions closed. Bye! (elapsed={elapsed:.2f}s)")
            else:
                logger.error(
                    f"⚠️ Shutdown incomplete! {remaining_count} positions remain. "
                    f"Errors: {errors}"
                )
            
            return result

    async def _wait_for_active_executions(self, errors: List[str]) -> None:
        """Wait for ParallelExecutionManager to gracefully stop active executions."""
        parallel_exec = self._components.get("parallel_exec")
        
        if not parallel_exec:
            logger.info("ℹ️ No ParallelExecutionManager registered")
            return
        
        active_count = len(getattr(parallel_exec, 'active_executions', {}))
        if active_count == 0:
            logger.info("✅ No active executions to wait for")
            return
        
        logger.info(f"⏳ Waiting for {active_count} active executions to complete or rollback...")
        
        try:
            # Give ParallelExecutionManager time to gracefully stop
            # This calls its stop() which handles rollback internally
            async with asyncio.timeout(45.0):  # 45s for graceful stop
                await parallel_exec.stop(force=False)
        except asyncio.TimeoutError:
            logger.warning("⚠️ Graceful execution stop timed out, forcing...")
            errors.append("execution_graceful_timeout")
            try:
                await parallel_exec.stop(force=True)
            except Exception as e:
                errors.append(f"execution_force_stop:{e}")
        except Exception as e:
            errors.append(f"execution_stop_error:{e}")
            logger.error(f"❌ Error stopping executions: {e}")

    async def _final_position_sweep(self, errors: List[str]) -> None:
        """
        Final safety check for any orphaned positions.
        
        This catches positions that:
        - Were opened by fills during shutdown
        - Were missed by earlier close attempts
        - Appeared due to race conditions
        """
        logger.info("🔍 Final position sweep...")
        
        lighter = self._components.get("lighter")
        x10 = self._components.get("x10")
        
        positions = await self._fetch_positions()
        
        all_positions = []
        for pos in positions.get("lighter", []):
            all_positions.append(("lighter", lighter, pos))
        for pos in positions.get("x10", []):
            all_positions.append(("x10", x10, pos))
        
        if not all_positions:
            logger.info("✅ Final sweep: No positions found")
            return
        
        logger.warning(f"⚠️ Final sweep found {len(all_positions)} positions to close!")
        
        for exchange_name, adapter, pos in all_positions:
            symbol = pos.get("symbol")
            size = self._safe_float(pos.get("size", 0))
            
            if abs(size) < 1e-8:
                continue
            
            if not adapter:
                errors.append(f"final_close_no_adapter:{exchange_name}:{symbol}")
                continue
            
            try:
                # original_side is the side of the POSITION (BUY for long, SELL for short)
                original_side = "BUY" if size > 0 else "SELL"
                close_side = "SELL" if size > 0 else "BUY"
                
                # Calculate notional_usd from size and cached price
                price = self._get_cached_price(symbol)
                if price:
                    notional_usd = abs(size) * price
                else:
                    notional_usd = abs(size) * 100  # Conservative fallback
                
                logger.info(f"🚨 Final close: {exchange_name} {symbol} {close_side} {abs(size)} (notional=${notional_usd:.2f})")
                
                # Use close_live_position with POSITIONAL arguments (symbol, original_side, notional_usd)
                if hasattr(adapter, 'close_live_position'):
                    success, _ = await adapter.close_live_position(
                        symbol,
                        original_side,
                        notional_usd
                    )
                else:
                    success, _ = await adapter.open_live_position(
                        symbol=symbol,
                        side=close_side,
                        notional_usd=0,
                        amount=abs(size),
                        reduce_only=True,
                        time_in_force="IOC"
                    )
                
                if not success:
                    errors.append(f"final_close_failed:{exchange_name}:{symbol}")
                    
            except Exception as e:
                errors.append(f"final_close_error:{exchange_name}:{symbol}:{e}")
                logger.error(f"❌ Final close error {symbol}: {e}")
        
        # Verify all closed
        await asyncio.sleep(1.0)
        final_check = await self._fetch_positions()
        final_count = len(final_check.get("lighter", [])) + len(final_check.get("x10", []))
        
        if final_count > 0:
            logger.error(f"❌ CRITICAL: {final_count} positions still remain after final sweep!")
        else:
            logger.info("✅ Final sweep complete - all positions closed")

    # ----- Phases -----------------------------------------------------
    async def _cancel_open_orders(self, errors: List[str]) -> None:
        """Cancel open orders for relevant symbols with timeout."""
        lighter = self._components.get("lighter")
        x10 = self._components.get("x10")

        symbols = await self._collect_relevant_symbols()
        if not symbols:
            logger.info("🔕 No relevant symbols found for cancellation")
            return

        # Phase 1: Try global cancel for Lighter first (if available)
        # This attempts to execute the optimized ImmediateCancelAll once.
        lighter_global_done = False
        if lighter and hasattr(lighter, "cancel_all_orders") and symbols:
            try:
                # Use first symbol to trigger global cancel attempt
                first_sym = symbols[0]
                logger.info("⚡ Attempting global Lighter cancel...")
                async with asyncio.timeout(10.0):
                    # Check if it returns True (success/deduplicated)
                    lighter_global_done = await lighter.cancel_all_orders(first_sym)
            except Exception as e:
                logger.warning(f"⚠️ Global Lighter cancel attempt failed: {e}")

        # Phase 2: Cancel remaining/others with increased timeout
        tasks = []
        for sym in symbols:
            # Only add Lighter cancel if global failed
            if lighter and hasattr(lighter, "cancel_all_orders") and not lighter_global_done:
                tasks.append(lighter.cancel_all_orders(sym))
            if x10 and hasattr(x10, "cancel_all_orders"):
                tasks.append(x10.cancel_all_orders(sym))

        if not tasks:
            return

        logger.info(f"🛑 Cancelling open orders for {len(symbols)} symbols (LighterGlobal={lighter_global_done})...")
        try:
            # Increased timeout to 15s to handle fallback loop if global cancel failed
            async with asyncio.timeout(15.0):  
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

            # Close Lighter positions with escalating slippage (price from WS cache first)
            lighter_tasks = []
            for pos in positions["lighter"]:
                size = self._safe_float(pos.get("size", 0))
                if size == 0:
                    continue
                symbol = pos.get("symbol")
                if not symbol:
                    continue
                close_side = "SELL" if size > 0 else "BUY"
                price = self._get_cached_price(symbol)

                # FIXED: Let the adapter handle slippage to avoid "accidental price" error 21733
                # The adapter has its own fat-finger protection that clamps to ~3% from mark
                # We pass the raw price and let the adapter apply LIGHTER_MAX_SLIPPAGE_PCT
                try:
                    logger.info(
                        f"🛑 Closing Lighter {symbol}: {close_side} {abs(size)} "
                        f"(IOC reduce_only, price={price})"
                    )
                    lighter_tasks.append(
                        lighter.open_live_position(
                            symbol=symbol,
                            side=close_side,
                            notional_usd=0,
                            amount=abs(size),
                            price=price,  # Raw price - adapter applies slippage
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
                    # FIXED: Need more time for sequential order sending (1-2s per order due to nonce)
                    # With 5 positions = 5-10 seconds just for order creation
                    async with asyncio.timeout(20.0):
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

        # Note: parallel_exec.stop() is already called in _wait_for_active_executions phase
        # No need to call it again here

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
        """Fetch positions from both exchanges with safety - OPTIMIZED for fast shutdown."""
        lighter = self._components.get("lighter")
        x10 = self._components.get("x10")
        positions = {"lighter": [], "x10": []}

        # OPTIMIZED: Fetch both in parallel for faster shutdown
        async def fetch_lighter():
            if lighter and hasattr(lighter, "fetch_open_positions"):
                try:
                    async with asyncio.timeout(3.0):  # OPTIMIZED: Reduced from 5s to 3s
                        return await lighter.fetch_open_positions() or []
                except Exception:
                    return []
            return []

        async def fetch_x10():
            if x10 and hasattr(x10, "fetch_open_positions"):
                try:
                    async with asyncio.timeout(3.0):  # OPTIMIZED: Reduced from 5s to 3s
                        return await x10.fetch_open_positions() or []
                except Exception:
                    return []
            return []

        # Use return_exceptions=True to prevent "exception was never retrieved" errors
        results = await asyncio.gather(fetch_lighter(), fetch_x10(), return_exceptions=True)
        
        # Handle potential exceptions in results
        lighter_pos = results[0] if not isinstance(results[0], Exception) else []
        x10_pos = results[1] if not isinstance(results[1], Exception) else []
        
        positions["lighter"] = lighter_pos
        positions["x10"] = x10_pos

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

    def _get_cached_price(self, symbol: str) -> Optional[float]:
        """Prefer WebSocket/cache price; do not call REST to avoid delays/rate limits."""
        lighter = self._components.get("lighter")
        if not lighter:
            return None
        try:
            price = lighter.get_price(symbol)
            return float(price) if price else None
        except Exception:
            return None


_SHUTDOWN_SINGLETON: Optional[ShutdownOrchestrator] = None


def get_shutdown_orchestrator() -> ShutdownOrchestrator:
    global _SHUTDOWN_SINGLETON
    if _SHUTDOWN_SINGLETON is None:
        _SHUTDOWN_SINGLETON = ShutdownOrchestrator()
    return _SHUTDOWN_SINGLETON

