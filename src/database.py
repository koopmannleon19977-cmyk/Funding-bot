# Compatibility shim - This file has been moved to infrastructure/persistence/
# Import from new location for better organization
from src.infrastructure.persistence.database import (
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

__all__ = [
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
]
