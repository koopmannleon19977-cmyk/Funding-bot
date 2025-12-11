# src/core/__init__.py
"""
Core bot logic modules.

This package contains the main trading logic split into logical modules:
- state: State management and DB wrappers
- opportunities: Opportunity detection
- trading: Trade execution
- trade_management: Open trade management
- monitoring: Background loops and watchdogs
- startup: Bot initialization and entry points
"""

from .state import (
    get_open_trades,
    add_trade_to_state,
    close_trade_in_state,
    archive_trade_to_history,
    get_cached_positions,
    get_execution_lock,
    get_symbol_lock,
    get_local_state_manager,
    check_total_exposure,
)

from .opportunities import (
    find_opportunities,
    calculate_expected_profit,
    is_tradfi_or_fx,
)

from .trading import (
    execute_trade_parallel,
    execute_trade_task,
    launch_trade_task,
    close_trade,
    safe_close_x10_position,
    get_actual_position_size,
    process_symbol,
)

from .trade_management import (
    sync_check_and_fix,
    cleanup_zombie_positions,
    calculate_trade_age,
    calculate_realized_pnl,
    should_farm_quick_exit,
    parse_iso_time,
    reconcile_db_with_exchanges,
    reconcile_state_with_exchange,
    get_cached_positions,
    manage_open_trades,
)

from .monitoring import (
    connection_watchdog,
    cleanup_finished_tasks,
    emergency_position_cleanup,
    farm_loop,
    trade_management_loop,
    maintenance_loop,
    health_reporter,
    balance_watchdog,
    logic_loop,
)

from .startup import (
    run_bot_v5,
    FundingBot,
    main_entry,
    setup_database,
    migrate_database,
    close_all_open_positions_on_start,
)

__all__ = [
    # State
    'get_open_trades',
    'add_trade_to_state',
    'close_trade_in_state',
    'archive_trade_to_history',
    'get_cached_positions',
    'get_execution_lock',
    'get_symbol_lock',
    'get_local_state_manager',
    'check_total_exposure',
    # Opportunities
    'find_opportunities',
    'calculate_expected_profit',
    'is_tradfi_or_fx',
    # Trading
    'execute_trade_parallel',
    'execute_trade_task',
    'launch_trade_task',
    'close_trade',
    'safe_close_x10_position',
    'get_actual_position_size',
    'process_symbol',
    # Trade Management
    'sync_check_and_fix',
    'cleanup_zombie_positions',
    'calculate_trade_age',
    'calculate_realized_pnl',
    'should_farm_quick_exit',
    'parse_iso_time',
    'manage_open_trades',
    'reconcile_db_with_exchanges',
    'reconcile_state_with_exchange',
    # Monitoring
    'connection_watchdog',
    'cleanup_finished_tasks',
    'emergency_position_cleanup',
    'farm_loop',
    'trade_management_loop',
    'maintenance_loop',
    'health_reporter',
    'balance_watchdog',
    'logic_loop',
    # Startup
    'run_bot_v5',
    'FundingBot',
    'main_entry',
    'setup_database',
    'migrate_database',
    'close_all_open_positions_on_start',
]


