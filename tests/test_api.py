import asyncio
import time
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import aiohttp
import pytest

from src.api_server import DashboardApi


# Mock Objects
class MockStateManager:
    pass


class MockParallelExec:
    def __init__(self):
        self.active_executions = {}
        self.state = MagicMock()
        self.state.value = "RUNNING"

    def get_execution_stats(self):
        return {"successful": 10, "failed": 0}


@pytest.mark.asyncio
async def test_api_endpoints():
    # Setup
    state_manager = MockStateManager()
    parallel_exec = MockParallelExec()

    # Mock an active execution
    mock_exec = MagicMock()
    mock_exec.state.value = "OPEN"
    mock_exec.pnl = Decimal("10.50")
    mock_exec.entry_time = datetime.now()
    parallel_exec.active_executions["BTC-USD"] = mock_exec

    api = DashboardApi(state_manager, parallel_exec, time.time())

    # Start API
    await api.start()

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Test /status
            async with session.get("http://localhost:8080/status") as resp:
                print(f"GET /status: {resp.status}")
                assert resp.status == 200
                data = await resp.json()
                print(data)
                assert data["status"] == "RUNNING"

            # 2. Test /pnl
            async with session.get("http://localhost:8080/pnl") as resp:
                print(f"GET /pnl: {resp.status}")
                assert resp.status == 200
                data = await resp.json()
                print(data)
                assert data["session_stats"]["successful"] == 10

            # 3. Test /positions
            async with session.get("http://localhost:8080/positions") as resp:
                print(f"GET /positions: {resp.status}")
                assert resp.status == 200
                data = await resp.json()
                print(data)
                assert len(data["positions"]) == 1
                assert data["positions"][0]["symbol"] == "BTC-USD"
                assert data["positions"][0]["pnl"] == 10.5

    finally:
        await api.stop()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.run_until_complete(test_api_endpoints())
