"""
Infrastructure persistence layer.

This package contains persistence infrastructure:
- Database connections and repositories
- State management
- Data models
"""

from .database import (
    AsyncDatabase,
    DBConfig,
    WriteOperation,
    TradeRepository,
    FundingRepository,
    ExecutionLogRepository,
    get_database,
    get_trade_repository,
    get_funding_repository,
    get_execution_repository,
    close_database,
)

from .state_manager import (
    InMemoryStateManager,
    TradeState,
    TradeStatus,
    get_state_manager,
    close_state_manager,
)

__all__ = [
    # Database
    'AsyncDatabase',
    'DBConfig',
    'WriteOperation',
    'TradeRepository',
    'FundingRepository',
    'ExecutionLogRepository',
    'get_database',
    'get_trade_repository',
    'get_funding_repository',
    'get_execution_repository',
    'close_database',
    # State Manager
    'InMemoryStateManager',
    'TradeState',
    'TradeStatus',
    'get_state_manager',
    'close_state_manager',
]
