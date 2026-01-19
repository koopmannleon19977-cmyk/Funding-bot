import asyncio
import json
import logging
import socket
import time
from datetime import datetime
from decimal import Decimal

from aiohttp import web

import config

logger = logging.getLogger(__name__)

# #region agent log
DEBUG_LOG_PATH = r"c:\Users\koopm\funding-bot\.cursor\debug.log"


def _write_debug_log(entry):
    """Write debug log entry to NDJSON file"""
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Fail silently to avoid breaking main flow


# #endregion


def json_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class DashboardApi:
    def __init__(self, state_manager, parallel_exec, start_time):
        self.state_manager = state_manager
        self.parallel_exec = parallel_exec
        self.start_time = start_time
        self.app = web.Application()
        self.runner = None
        self.site = None

        # Routes
        self.app.router.add_get("/status", self.handle_status)
        self.app.router.add_get("/pnl", self.handle_pnl)
        self.app.router.add_get("/positions", self.handle_positions)
        self.app.router.add_get("/health", self.handle_health)

    async def start(self) -> bool:
        if not getattr(config, "API_ENABLED", False):
            logger.info("🚫 API Disabled in config.")
            return False

        host = getattr(config, "API_HOST", "0.0.0.0")
        port = getattr(config, "API_PORT", 8080)

        # #region agent log
        _write_debug_log(
            {
                "sessionId": "debug-session",
                "runId": "port-binding-debug",
                "hypothesisId": "C",
                "location": "api_server.py:start",
                "message": "Port check before start",
                "data": {"host": host, "port": port, "port_available": None},
                "timestamp": int(time.time() * 1000),
            }
        )
        # #endregion

        # Check if port is available (Hypothesis C)
        port_available = self._check_port_available(host, port)
        # #region agent log
        _write_debug_log(
            {
                "sessionId": "debug-session",
                "runId": "port-binding-debug",
                "hypothesisId": "C",
                "location": "api_server.py:start",
                "message": "Port availability check result",
                "data": {"port_available": port_available, "port": port},
                "timestamp": int(time.time() * 1000),
            }
        )
        # #endregion

        if not port_available:
            logger.warning(
                f"⚠️ Port {port} appears to be in use. "
                "Dashboard API will not start; change API_PORT or stop the other process."
            )
            return False

        logger.info(f"🌐 Starting Dashboard API at http://{host}:{port}")

        # #region agent log
        _write_debug_log(
            {
                "sessionId": "debug-session",
                "runId": "port-binding-debug",
                "hypothesisId": "A",
                "location": "api_server.py:start",
                "message": "Before runner.setup()",
                "data": {"runner_exists": self.runner is not None},
                "timestamp": int(time.time() * 1000),
            }
        )
        # #endregion

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)

        # #region agent log
        _write_debug_log(
            {
                "sessionId": "debug-session",
                "runId": "port-binding-debug",
                "hypothesisId": "A",
                "location": "api_server.py:start",
                "message": "Before site.start()",
                "data": {"site_exists": self.site is not None, "runner_exists": self.runner is not None},
                "timestamp": int(time.time() * 1000),
            }
        )
        # #endregion

        try:
            await self.site.start()
            # #region agent log
            _write_debug_log(
                {
                    "sessionId": "debug-session",
                    "runId": "port-binding-debug",
                    "hypothesisId": "A",
                    "location": "api_server.py:start",
                    "message": "site.start() succeeded",
                    "data": {"site_exists": self.site is not None},
                    "timestamp": int(time.time() * 1000),
                }
            )
            # #endregion
        except Exception as e:
            # #region agent log
            _write_debug_log(
                {
                    "sessionId": "debug-session",
                    "runId": "port-binding-debug",
                    "hypothesisId": "A",
                    "location": "api_server.py:start",
                    "message": "site.start() failed - cleaning up",
                    "data": {"error_type": type(e).__name__, "error_msg": str(e), "port_was_available": port_available},
                    "timestamp": int(time.time() * 1000),
                }
            )
            # #endregion
            # FIX: Cleanup on failure to prevent port binding issues
            try:
                if self.site:
                    await self.site.stop()
                if self.runner:
                    await self.runner.cleanup()
            except Exception as cleanup_error:
                logger.warning(f"⚠️ Error during cleanup after start failure: {cleanup_error}")
            self.site = None
            self.runner = None
            raise

        return True

    def _check_port_available(self, host, port):
        """Check if port is available (Hypothesis C)"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                result = sock.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port))
                return result != 0  # Port is available if connection fails
        except Exception:
            return True  # Assume available if check fails

    async def stop(self):
        # #region agent log
        _write_debug_log(
            {
                "sessionId": "debug-session",
                "runId": "port-binding-debug",
                "hypothesisId": "B",
                "location": "api_server.py:stop",
                "message": "stop() called",
                "data": {"runner_exists": self.runner is not None, "site_exists": self.site is not None},
                "timestamp": int(time.time() * 1000),
            }
        )
        # #endregion

        if self.runner:
            logger.info("🛑 Stopping Dashboard API...")

            # #region agent log
            _write_debug_log(
                {
                    "sessionId": "debug-session",
                    "runId": "port-binding-debug",
                    "hypothesisId": "A",
                    "location": "api_server.py:stop",
                    "message": "Before site.stop()",
                    "data": {"site_exists": self.site is not None},
                    "timestamp": int(time.time() * 1000),
                }
            )
            # #endregion

            # Hypothesis A: Explicitly stop site before cleanup
            if self.site:
                try:
                    await self.site.stop()
                    # #region agent log
                    _write_debug_log(
                        {
                            "sessionId": "debug-session",
                            "runId": "port-binding-debug",
                            "hypothesisId": "A",
                            "location": "api_server.py:stop",
                            "message": "site.stop() completed",
                            "data": {},
                            "timestamp": int(time.time() * 1000),
                        }
                    )
                    # #endregion
                except Exception as e:
                    # #region agent log
                    _write_debug_log(
                        {
                            "sessionId": "debug-session",
                            "runId": "port-binding-debug",
                            "hypothesisId": "A",
                            "location": "api_server.py:stop",
                            "message": "site.stop() failed",
                            "data": {"error_type": type(e).__name__, "error_msg": str(e)},
                            "timestamp": int(time.time() * 1000),
                        }
                    )
                    # #endregion
                    logger.warning(f"⚠️ Error stopping site: {e}")

            # #region agent log
            _write_debug_log(
                {
                    "sessionId": "debug-session",
                    "runId": "port-binding-debug",
                    "hypothesisId": "A",
                    "location": "api_server.py:stop",
                    "message": "Before runner.cleanup()",
                    "data": {},
                    "timestamp": int(time.time() * 1000),
                }
            )
            # #endregion

            await self.runner.cleanup()

            # #region agent log
            _write_debug_log(
                {
                    "sessionId": "debug-session",
                    "runId": "port-binding-debug",
                    "hypothesisId": "A",
                    "location": "api_server.py:stop",
                    "message": "runner.cleanup() completed",
                    "data": {},
                    "timestamp": int(time.time() * 1000),
                }
            )
            # #endregion

            self.site = None
            self.runner = None
        else:
            # #region agent log
            _write_debug_log(
                {
                    "sessionId": "debug-session",
                    "runId": "port-binding-debug",
                    "hypothesisId": "B",
                    "location": "api_server.py:stop",
                    "message": "stop() called but runner is None",
                    "data": {},
                    "timestamp": int(time.time() * 1000),
                }
            )
            # #endregion

    async def handle_health(self, request):
        return web.Response(text="OK")

    async def handle_status(self, request):
        uptime = time.time() - self.start_time
        data = {
            "status": "RUNNING",
            "uptime_seconds": uptime,
            "uptime_human": f"{uptime / 3600:.2f} hours",
            "version": "5.0.0",
            "tasks_active": len(asyncio.all_tasks()),
            "farm_mode": getattr(config, "FARM_MODE", False),
        }
        return web.json_response(data, dumps=lambda x: json.dumps(x, default=json_serializer))

    async def handle_pnl(self, request):
        # Get stats from StateManager or ParallelExec
        stats = self.parallel_exec.get_execution_stats()
        # You might want to aggregate DB stats here if available

        data = {
            "session_stats": stats,
            "total_pnl_usd": 0.00,  # TODO: Connect to explicit PnL tracker if available
            "win_rate": 0.0,  # Placeholder
        }
        return web.json_response(data, dumps=lambda x: json.dumps(x, default=json_serializer))

    async def handle_positions(self, request):
        # Get active executions from ParallelExecutionManager
        executions = []
        for symbol, exc in self.parallel_exec.active_executions.items():
            executions.append(
                {
                    "symbol": symbol,
                    "state": exc.state.value,
                    "pnl": float(exc.pnl) if exc.pnl else 0.0,
                    "entry_time": exc.entry_time.isoformat() if exc.entry_time else None,
                }
            )

        # Optional: Get raw exchange positions if adapters expose them
        # x10_pos = await self.parallel_exec.x10.fetch_open_positions()

        data = {"count": len(executions), "positions": executions}
        return web.json_response(data, dumps=lambda x: json.dumps(x, default=json_serializer))
