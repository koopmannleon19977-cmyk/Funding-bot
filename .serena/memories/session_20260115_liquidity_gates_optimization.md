# Session 2026-01-15: Liquidity Gates Optimization

## Problem Discovered
Bot was rejecting ALL trading opportunities due to overly strict liquidity gate thresholds.

### Root Cause
The `hedge_depth_preflight_multiplier: 8.0` in config.yaml was multiplying ALL liquidity gate parameters:

| Parameter | Base Value | After 8x Multiplier | Effect |
|-----------|------------|---------------------|--------|
| `min_l1_notional_usd` | $500 | $4,000 | L1 must have $4,000 notional |
| `min_l1_notional_multiple` | 1.2x | 9.6x | L1 must be 9.6x trade size |
| `max_l1_qty_utilization` | 70% | 8.75% | Trade can only use 8.75% of L1 |

### Code Location
The multiplication happens in `src/funding_bot/services/opportunities/evaluator.py:227-237`:
```python
if es and getattr(es, "hedge_depth_preflight_enabled", False):
    mult = hedge_depth_preflight_multiplier  # Was 8.0
    min_l1_usd = min_l1_usd * mult
    min_l1_mult = min_l1_mult * mult
    max_l1_util = max_l1_util / mult  # Inverted!
```

## Solution Applied
Changed `hedge_depth_preflight_multiplier` from `8.0` to `2.0` in `config.yaml:268`.

### New Effective Thresholds
| Parameter | Old (8x) | New (2x) |
|-----------|----------|----------|
| `min_l1_notional_usd` | $4,000 | $1,000 |
| `min_l1_notional_multiple` | 9.6x | 2.4x |
| `max_l1_qty_utilization` | 8.75% | 35% |

## Key Files
- `src/funding_bot/config/config.yaml:268` - hedge_depth_preflight_multiplier
- `src/funding_bot/services/opportunities/evaluator.py` - Multiplier application
- `src/funding_bot/services/liquidity_gates_l1.py` - L1 depth gate logic
- `src/funding_bot/services/liquidity_gates_preflight.py` - Preflight checks

## Best Practices Learned
1. **Test with realistic market data**: The 8x multiplier was too conservative for altcoin liquidity
2. **Monitor rejection rates**: If ALL opportunities are rejected, check gate thresholds
3. **Balance safety vs opportunity**: 2x multiplier provides safety buffer while allowing trades

## Related Memories
- `orderbook_depth_low_volume_symbols` - Low volume symbol handling
- `x10_integration_critical_parameters` - X10 specific parameters

## Status
- [x] Root cause identified
- [x] Solution implemented (multiplier 8.0 → 2.0)
- [x] Tests passing (301 unit tests)
- [ ] Live verification pending (requires bot restart)
