# Pre-Flight Liquidity Check Implementation Plan

**Created**: 2026-01-15
**Priority**: HIGH - Solves critical rollback problem
**Status**: Planning Phase

---

## Executive Summary

**Problem**: ~37% of trades roll back because Leg1 executes (Lighter) but Leg2 fails (X10) due to insufficient liquidity.

**Root Cause**: No pre-flight liquidity validation before executing Leg1.

**Solution**: Implement multi-level pre-flight liquidity checks on BOTH exchanges before any order execution.

**Expected Impact**: Reduce rollback rate from 37% to <5%, improving success rate by ~32%.

---

## Research Findings

### Lighter Exchange API

**Orderbook Endpoint**: `GET /api/v1/info/order_book_details`

**Authentication**: API key + private key signing (asymmetric)

**Response Format** (based on SDK):
```python
{
    "bids": [
        {"price": "50000.0", "qty": "1.5"},
        {"price": "49999.0", "qty": "2.0"},
        # ... more levels
    ],
    "asks": [
        {"price": "50001.0", "qty": "1.2"},
        {"price": "50002.0", "qty": "1.8"},
        # ... more levels
    ]
}
```

**Rate Limits**:
- Standard: 60 weighted requests/minute
- Premium: 24,000 weighted requests/minute (400x more capacity)

**Depth Capabilities**: Not explicitly documented - need empirical testing

**WebSocket Streams**: Available for real-time orderbook updates

---

### X10/Extended Exchange API

**Orderbook Endpoint**: `GET /api/v1/info/markets/{market}/orderbook`

**Authentication**: API key (Bearer token)

**Response Format**:
```json
{
  "bid": [
    {"qty": "0.04852", "price": "61827.7"},
    {"qty": "0.04852", "price": "61827.6"}
  ],
  "ask": [
    {"qty": "0.04852", "price": "61840.3"},
    {"qty": "0.04852", "price": "61840.4"}
  ]
}
```

**Rate Limits**:
- Standard: 1,000 requests/minute
- Market Maker: 60,000 requests/5 minutes

**Depth Capabilities**: Example shows 2 levels, likely supports more

**WebSocket Streams**: Available via WebSocket subscription

---

### Existing Infrastructure Analysis

**File**: `src/funding_bot/services/liquidity_gates.py`

**Current Capabilities**:
1. **L1 Depth Check** (`liquidity_gates_l1.py`):
   - `calculate_l1_depth_cap()` - Returns top-of-book quantity
   - `check_l1_depth_for_entry()` - Validates single-level depth
   - `check_x10_l1_compliance()` - X10-specific L1 check

2. **Impact-Based Depth Check** (`liquidity_gates_impact_gates.py`):
   - `calculate_depth_cap_by_impact()` - Simulates price impact
   - `check_depth_for_entry_by_impact()` - Validates impact tolerance
   - `check_x10_depth_compliance_by_impact()` - X10 impact check

**Problem**: These checks happen in execution flow (AFTER Leg1 decision), not pre-flight.

---

## Implementation Plan

### Phase 1: Pre-Flight Liquidity Check Function

**File**: `src/funding_bot/services/liquidity_gates_preflight.py`

**Purpose**: Validate liquidity on BOTH exchanges BEFORE executing Leg1.

**Key Features**:
1. Multi-level depth aggregation (configurable levels: 3-10)
2. Safety factor application (default 3x required quantity)
3. Spread validation (must be within acceptable range)
4. Cached orderbook data with TTL (5 seconds)
5. Fallback to L1 if multi-level fails
6. Comprehensive logging for monitoring

**Function Signature**:
```python
async def check_preflight_liquidity(
    lighter_adapter: LighterAdapter,
    x10_adapter: X10Adapter,
    symbol: str,
    required_qty: Decimal,
    config: PreflightLiquidityConfig,
) -> PreflightLiquidityResult:
    """
    Check if both exchanges have sufficient liquidity BEFORE executing any orders.
    
    Args:
        lighter_adapter: Lighter exchange adapter
        x10_adapter: X10 exchange adapter
        symbol: Trading symbol (e.g., "BTC-PERP")
        required_qty: Quantity needed for the trade
        config: Configuration for checks
    
    Returns:
        PreflightLiquidityResult with pass/fail and detailed metrics
    """
```

**Configuration** (`src/funding_bot/config/settings.py`):
```yaml
preflight_liquidity:
  enabled: true
  depth_levels: 5              # Number of levels to aggregate
  safety_factor: 3.0           # Require 3x available liquidity
  max_spread_bps: 50           # Max acceptable spread (0.5%)
  orderbook_cache_ttl_seconds: 5.0
  fallback_to_l1: true         # Use L1 if multi-level fails
  min_liquidity_threshold: 0.01 # Minimum quantity to consider liquid
```

---

### Phase 2: Integration Into Execution Flow

**File**: `src/funding_bot/services/execution/flow.py`

**Integration Point**: BEFORE `_execute_leg1()` decision.

**Flow Changes**:
```python
# Current flow:
opportunity = await self._scan_opportunities()
if opportunity:
    await self._execute_leg1(opportunity)  # ← May rollback!

# New flow:
opportunity = await self._scan_opportunities()
if opportunity:
    # NEW: Pre-flight liquidity check
    preflight_result = await self._check_preflight_liquidity(
        opportunity.symbol,
        opportunity.quantity
    )
    
    if not preflight_result.passed:
        logger.info(
            f"Skipping trade (preflight failed): {preflight_result.failure_reason}",
            extra={
                "symbol": opportunity.symbol,
                "required_qty": str(opportunity.quantity),
                "lighter_available": str(preflight_result.lighter_available_qty),
                "x10_available": str(preflight_result.x10_available_qty),
            }
        )
        continue  # Skip to next opportunity
    
    # Safe to execute
    await self._execute_leg1(opportunity)
```

**Fallback Behavior**:
- If pre-flight fails, log detailed metrics for analysis
- Continue scanning for other opportunities
- No rollback triggered (because no order executed)

---

### Phase 3: Unit Tests (Offline Mocks)

**File**: `tests/unit/services/liquidity/test_preflight_liquidity.py`

**Test Cases**:
1. **Happy Path**: Both exchanges have sufficient liquidity
2. **Lighter Insufficient**: Lighter fails liquidity check
3. **X10 Insufficient**: X10 fails liquidity check
4. **Spread Too Wide**: Spread exceeds max_spread_bps
5. **Fallback to L1**: Multi-level fails, L1 succeeds
6. **Cache Hit**: Uses cached orderbook data
7. **Cache Miss**: Fetches fresh orderbook data
8. **Safety Factor Applied**: Correctly multiplies required qty

**Mock Strategy**:
```python
from unittest.mock import AsyncMock, patch

@pytest.mark.unit
async def test_preflight_liquidity_both_exchanges_sufficient():
    # Arrange
    lighter_adapter = AsyncMock()
    x10_adapter = AsyncMock()
    
    lighter_adapter.get_orderbook.return_value = Orderbook(
        bids=[Level(price="50000", qty="5.0") for _ in range(10)],
        asks=[Level(price="50001", qty="5.0") for _ in range(10)]
    )
    
    x10_adapter.get_orderbook.return_value = Orderbook(
        bids=[Level(price="50000", qty="5.0") for _ in range(10)],
        asks=[Level(price="50001", qty="5.0") for _ in range(10)]
    )
    
    required_qty = Decimal("1.0")
    config = PreflightLiquidityConfig(depth_levels=5, safety_factor=3.0)
    
    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, "BTC-PERP", required_qty, config
    )
    
    # Assert
    assert result.passed is True
    assert result.lighter_available_qty >= required_qty * config.safety_factor
    assert result.x10_available_qty >= required_qty * config.safety_factor
```

**Coverage Target**: >95% for pre-flight logic

---

### Phase 4: Integration Tests (Real API Calls)

**File**: `tests/integration/liquidity/test_preflight_liquidity_live.py`

**Test Prerequisites**:
- Live API keys for Lighter and X10
- Testnet or paper trading environment
- Small quantities (no real money at risk)

**Test Cases**:
1. **Real Orderbook Fetch**: Fetch actual orderbooks from both exchanges
2. **Depth Level Discovery**: Test how many levels each exchange returns
3. **Latency Measurement**: Measure time to fetch both orderbooks
4. **Spread Validation**: Validate real spread data
5. **Liquidity Aggregation**: Test multi-level aggregation on real data
6. **Cache Performance**: Validate cache TTL and invalidation

**Example Test**:
```python
@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("LIGHTER_API_KEY") or not os.getenv("X10_API_KEY"),
    reason="Missing API keys"
)
async def test_preflight_liquidity_real_orderbooks():
    # Arrange
    lighter_adapter = await create_lighter_adapter()
    x10_adapter = await create_x10_adapter()
    
    symbol = "BTC-PERP"
    required_qty = Decimal("0.01")  # Small quantity for testing
    config = PreflightLiquidityConfig(depth_levels=5, safety_factor=3.0)
    
    # Act
    result = await check_preflight_liquidity(
        lighter_adapter, x10_adapter, symbol, required_qty, config
    )
    
    # Assert
    assert result is not None
    logger.info(f"Lighter available: {result.lighter_available_qty}")
    logger.info(f"X10 available: {result.x10_available_qty}")
    logger.info(f"Spread: {result.spread_bps} bps")
    
    # Document actual depth levels returned
    logger.info(f"Lighter levels: {result.lighter_depth_levels}")
    logger.info(f"X10 levels: {result.x10_depth_levels}")
```

**Discovery Experiments**:
```python
@pytest.mark.integration
async def test_discover_max_depth_levels():
    """Empirically determine how many levels each exchange returns."""
    lighter_adapter = await create_lighter_adapter()
    x10_adapter = await create_x10_adapter()
    
    # Fetch orderbooks and count levels
    lighter_ob = await lighter_adapter.get_orderbook("BTC-PERP")
    x10_ob = await x10_adapter.get_orderbook("BTC-PERP")
    
    logger.info(f"Lighter levels: {len(lighter_ob.bids)} bids, {len(lighter_ob.asks)} asks")
    logger.info(f"X10 levels: {len(x10_ob.bids)} bids, {len(x10_ob.asks)} asks")
    
    # Return findings for config tuning
    return {
        "lighter_max_levels": max(len(lighter_ob.bids), len(lighter_ob.asks)),
        "x10_max_levels": max(len(x10_ob.bids), len(x10_ob.asks))
    }
```

---

## Depth Level Strategy

### Recommended Configuration

**Based on Research and Industry Best Practices**:

| Symbol Category | Depth Levels | Safety Factor | Rationale |
|----------------|--------------|---------------|-----------|
| **High Volume** (BTC, ETH) | 3 levels | 2.0x | Deep liquidity, less slippage |
| **Medium Volume** | 5 levels | 3.0x | Moderate liquidity, standard safety |
| **Low Volume** | 10 levels | 5.0x | Shallow liquidity, extra caution |

**Aggregation Formula**:
```python
# Sum top N levels of orderbook
available_liquidity = sum(level.qty for level in orderbook.asks[:depth_levels])

# Apply safety factor
required_liquidity = trade_qty * safety_factor

# Check condition
if available_liquidity < required_liquidity:
    return PreflightResult(passed=False, reason="Insufficient liquidity")
```

### Level Discovery Approach

**Phase 4 Integration Tests will answer**:
1. How many levels does Lighter actually return? (Test with real API)
2. How many levels does X10 actually return? (Test with real API)
3. What's the latency impact of fetching more levels? (Benchmark)
4. How stale is orderbook data after 5 seconds? (Cache validation)

**Config Tuning Process**:
1. Run discovery experiments in Phase 4
2. Analyze results to determine optimal levels
3. Update default config in `settings.py`
4. Document findings in `KNOWLEDGE.md`

---

## Monitoring & Alerting

### Metrics to Track

**New Metrics** (`src/funding_bot/observability/metrics.py`):
```python
class PreflightLiquidityMetrics:
    preflight_checks_total: Counter
    preflight_checks_passed: Counter
    preflight_checks_failed: Counter
    preflight_failure_reason: Histogram
    preflight_latency_seconds: Histogram
    preflight_cache_hit_rate: Gauge
    preflight_depth_levels: Gauge
    preflight_available_liquidity: Gauge
    preflight_spread_bps: Gauge
```

### Log Events

**Structured Logging**:
```python
{
    "event": "preflight_liquidity_check",
    "symbol": "BTC-PERP",
    "required_qty": "1.0",
    "lighter_available": "15.0",
    "x10_available": "12.0",
    "spread_bps": 20,
    "depth_levels": 5,
    "safety_factor": 3.0,
    "passed": true,
    "latency_ms": 45
}

{
    "event": "preflight_liquidity_failed",
    "symbol": "ETH-PERP",
    "required_qty": "10.0",
    "lighter_available": "5.0",
    "x10_available": "25.0",
    "failure_reason": "Lighter insufficient liquidity (need 30.0, have 5.0)",
    "passed": false
}
```

### Alerts

**Warning Conditions**:
1. Pre-flight failure rate > 20% (may need config tuning)
2. Average pre-flight latency > 500ms (performance issue)
3. Cache hit rate < 50% (cache may not be working)
4. Spread > max_spread_bps frequently (market condition change)

---

## Risk Mitigation

### Potential Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| **Stale orderbook data** | False confidence in liquidity | 5-second cache TTL, timestamp validation |
| **Rate limit exhaustion** | Failed pre-flight checks | Use Premium API keys, implement backoff |
| **Over-conservative checks** | Missed profitable opportunities | Configurable safety factor, monitor skip rate |
| **WebSocket disconnection** | No real-time depth data | Fallback to REST polling, circuit breaker |
| **Multi-exchange latency** | Data not synchronized | Fetch both in parallel, use max timestamp |

### Rollback Prevention

**Even with pre-flight checks, rollbacks can still occur** (race conditions, rapid liquidity changes).

**Defense in Depth**:
1. **Pre-flight check** (new): Filter out obvious liquidity issues
2. **Fast execution** (existing): Execute both legs within milliseconds
3. **Rollback handler** (existing): Graceful rollback if Leg2 fails
4. **Broken hedge detection** (existing): Close if hedge persists >30s

---

## Implementation Timeline

### Phase 1: Pre-Flight Function (2-3 hours)
- [ ] Create `liquidity_gates_preflight.py`
- [ ] Implement multi-level aggregation
- [ ] Add configuration to `settings.py`
- [ ] Add unit tests (offline mocks)

### Phase 2: Integration (1-2 hours)
- [ ] Modify `execution_flow.py` to call pre-flight check
- [ ] Update logging and metrics
- [ ] Test in paper trading mode

### Phase 3: Unit Tests (1-2 hours)
- [ ] Create `test_preflight_liquidity.py`
- [ ] Implement 8+ test cases
- [ ] Achieve >95% coverage

### Phase 4: Integration Tests (2-3 hours)
- [ ] Create `test_preflight_liquidity_live.py`
- [ ] Run discovery experiments
- [ ] Benchmark latency and depth
- [ ] Tune configuration based on findings

### Phase 5: Documentation & Monitoring (1 hour)
- [ ] Update `KNOWLEDGE.md` with findings
- [ ] Add metrics to observability system
- [ ] Create alerts for warning conditions

**Total Estimated Time**: 7-11 hours

---

## Success Criteria

### Quantitative Metrics

| Metric | Current | Target | Measurement |
|--------|---------|--------|-------------|
| **Rollback Rate** | ~37% | <5% | Trade logs |
| **Pre-Flight Pass Rate** | N/A | >80% | Pre-flight logs |
| **Pre-Flight Latency** | N/A | <500ms | Metrics |
| **Cache Hit Rate** | N/A | >70% | Metrics |
| **Test Coverage** | N/A | >95% | pytest-cov |

### Qualitative Outcomes

- [ ] No increase in missed opportunities
- [ ] Reduced stress on execution flow (fewer rollbacks)
- [ ] Better understanding of exchange liquidity characteristics
- [ ] Improved confidence in trade execution

---

## Open Questions

1. **How many levels do exchanges actually return?** → Phase 4 discovery tests
2. **What's the optimal safety factor?** → Start with 3.0, tune based on results
3. **Should we use WebSocket for real-time depth?** → Phase 2+ feature
4. **How do we handle asymmetric liquidity?** → Use min(both exchanges) for sizing

---

## References

- **Lighter API**: https://apidocs.lighter.xyz/
- **X10/Extended API**: https://api.docs.extended.exchange/
- **Existing Liquidity Gates**: `src/funding_bot/services/liquidity_gates.py`
- **Execution Flow**: `src/funding_bot/services/execution/flow.py`
- **Rollback Handler**: `src/funding_bot/services/execution_rollback.py`

---

**Next Step**: Begin Phase 1 implementation after user approval.
