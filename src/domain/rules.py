from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Constitution:
    min_apy_filter: Decimal = Decimal("0.20")
    max_spread_filter_percent: Decimal = Decimal("0.002")
    min_profit_exit_usd: Decimal = Decimal("0.10")
    max_breakeven_hours: Decimal = Decimal("8")
    min_maintenance_apy: Decimal = Decimal("0.20")
    funding_flip_hours_threshold: Decimal = Decimal("4")
    volatility_panic_threshold: Decimal = Decimal("8")
    max_hold_hours: Decimal = Decimal("72")
    max_drawdown_pct: Decimal = Decimal("0.20")
    max_volatility_pct: Decimal = Decimal("50")
    notional_tolerance: Decimal = Decimal("0.001")
