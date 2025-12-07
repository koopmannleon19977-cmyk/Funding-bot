
import asyncio
import logging
import time
from aiohttp import web
import json
from decimal import Decimal
from datetime import datetime

import config

logger = logging.getLogger(__name__)

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
        self.app.router.add_get('/status', self.handle_status)
        self.app.router.add_get('/pnl', self.handle_pnl)
        self.app.router.add_get('/positions', self.handle_positions)
        self.app.router.add_get('/health', self.handle_health)

    async def start(self):
        if not getattr(config, 'API_ENABLED', False):
            logger.info("🚫 API Disabled in config.")
            return

        host = getattr(config, 'API_HOST', '0.0.0.0')
        port = getattr(config, 'API_PORT', 8080)

        logger.info(f"🌐 Starting Dashboard API at http://{host}:{port}")
        
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()

    async def stop(self):
        if self.runner:
            logger.info("🛑 Stopping Dashboard API...")
            await self.runner.cleanup()

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
            "farm_mode": getattr(config, 'FARM_MODE', False)
        }
        return web.json_response(data, dumps=lambda x: json.dumps(x, default=json_serializer))

    async def handle_pnl(self, request):
        # Get stats from StateManager or ParallelExec
        stats = self.parallel_exec.get_execution_stats()
        # You might want to aggregate DB stats here if available
        
        data = {
            "session_stats": stats,
            "total_pnl_usd": 0.00, # TODO: Connect to explicit PnL tracker if available
            "win_rate": 0.0 # Placeholder
        }
        return web.json_response(data, dumps=lambda x: json.dumps(x, default=json_serializer))

    async def handle_positions(self, request):
        # Get active executions from ParallelExecutionManager
        executions = []
        for symbol, exc in self.parallel_exec.active_executions.items():
             executions.append({
                 "symbol": symbol,
                 "state": exc.state.value,
                 "pnl": float(exc.pnl) if exc.pnl else 0.0,
                 "entry_time": exc.entry_time.isoformat() if exc.entry_time else None
             })
             
        # Optional: Get raw exchange positions if adapters expose them
        # x10_pos = await self.parallel_exec.x10.fetch_open_positions()
        
        data = {
            "count": len(executions),
            "positions": executions
        }
        return web.json_response(data, dumps=lambda x: json.dumps(x, default=json_serializer))
