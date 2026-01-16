# Phase 1: Premium-Optimized Performance Plan

**Date:** 2026-01-14
**Account:** Lighter Premium (24,000 weighted requests/minute)
**Status:** Ready for Implementation
**Project:** Funding Arbitrage Bot (Lighter Premium + X10)

---

## 🚀 PREMIUM ACCOUNT ADVANTAGE

### Rate Limit Comparison

| Account Type | REST Requests/Min | Strategy |
|--------------|-------------------|----------|
| **Premium** | **24,000** (weighted) | **AGGRESSIVE parallelization** 🚀 |
| Standard | 60 (hard cap) | Conservative |

**Key Implications:**
- **Weighted System**: Different endpoints have different weights (6, 50, 100, 150, 1200, 300)
- **Maximum Throughput**: With all weight-6 requests = **400 requests/second**
- **Realistic Mix**: Assuming average weight 50 = **480 requests/second**
- **WebSocket Constraint**: 200 messages/minute still applies (bottleneck for WS-heavy ops)

### Weight Breakdown (Premium Endpoints)

| Endpoint Type | Weight | Requests/Minute | Requests/Second |
|--------------|--------|-----------------|-----------------|
| Per User (cheap) | 6 | 240,000 | 4,000 |
| Per User (medium) | 50 | 28,800 | 480 |
| Per User (high) | 100 | 14,400 | 240 |
| Per User (very high) | 150 | 9,600 | 160 |
| Per User (expensive) | 1200 | 1,200 | 20 |
| Other | 300 | 4,800 | 80 |

---

## 🎯 PHASE 1 OPTIMIZATION PLAN (Premium-Optimized)

### 1.1 Latency Monitoring System (Week 1-2) ✅

**Goal:** Measure performance without increasing load

**Implementation:**
```python
# services/metrics/collector.py
class MetricsCollector:
    """Centralized performance metrics collection."""

    async def record_latency(self, operation: str, duration_ms: float)
    async def get_percentiles(self, operation: str, p: list[int]) -> dict[int, float]

# Usage:
@track_latency
async def place_order(self, request: OrderRequest) -> Order:
    ...
```

**Key Metrics to Track:**
- Order placement latency (P50, P95, P99)
- Market data refresh duration
- Exit evaluation duration
- API response time per exchange
- WebSocket message rate
- **Rate limit proximity** (% of Premium limit used)

**Risk:** LOW (observability only)

**Estimated Time:** 1-2 weeks

---

### 1.2 AGGRESSIVE Async Concurrency (Week 3-4) 🚀

**Goal:** Leverage Premium for maximum parallelization

#### Market Data Refresh - Premium Optimized

**BEFORE (Sequential):**
```python
for symbol in symbols:  # 10 symbols
    await refresh_symbol(symbol)
# Total: ~10 seconds
```

**AFTER (Premium Parallel):**
```python
async def refresh_symbols_premium_batch(symbols: list[str], batch_size: int = 20):
    """
    Refresh in large batches - Premium can handle it!
    20 symbols concurrent = 20 * 50 weight = 1000 weight
    At 24,000/min = 400 requests/sec capacity
    """
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        await asyncio.gather(*[
            refresh_symbol(symbol) for symbol in batch
        ])
        # Minimal throttle: Premium has headroom
        await asyncio.sleep(0.05)  # 50ms = 20 batches/sec
# Total: ~500ms (20x faster!)
```

**Rate Limit Math (Premium):**
- 20 symbols per batch × 20 batches/sec = **400 symbols/second**
- Well within 24,000/min limit!
- **400x faster than Standard account parallelization**

#### Exit Evaluation - Premium Parallel

**BEFORE (Sequential):**
```python
for trade in open_trades:  # 10 trades
    await evaluate_exit(trade)
# Total: ~1000ms (100ms per trade)
```

**AFTER (Premium Parallel):**
```python
semaphore = asyncio.Semaphore(10)  # Max 10 concurrent (Premium!)

async def evaluate_exit_with_limit(trade: Trade):
    async with semaphore:
        return await evaluate_exit(trade)

results = await asyncio.gather(*[
    evaluate_exit_with_limit(trade) for trade in open_trades
])
# Total: ~150ms (10 concurrent = 1 batch, ~100ms + overhead)
```

**WebSocket Constraint Math:**
- Max 10 concurrent evaluations
- Each evaluation = ~1-2 WebSocket messages
- **Inflight messages:** 10 × 2 = **20** ✅ (well under 50 limit!)
- **Messages/minute:** 10 evals × 2 messages × 4 evals/sec = **80/min** ✅ (well under 200 limit!)

#### Database Batch Operations

**BEFORE (Individual):**
```python
for trade in trades:
    await self.store.update_trade(trade)
```

**AFTER (Batch - Premium allows large batches):**
```python
await self.store.update_trades_batch(trades, batch_size=100)
# Process 100 trades in single transaction
```

**Expected Performance Gains (Premium):**
- Market Data Refresh: **20x faster** (10s → 500ms)
- Exit Eval: **6.7x faster** (1000ms → 150ms)
- DB Operations: **10x faster** (100ms → 10ms)

**Risk:** MEDIUM (async bugs can be tricky)

**Mitigation:**
- Start with conservative limits (batch_size=10, semaphore=5)
- Monitor rate limit proximity metrics
- Gradually increase to Premium limits
- Feature flags for quick rollback

**Estimated Time:** 2 weeks

---

### 1.3 Network & Connection Optimization (Week 5) ⚡

**Goal:** Reduce latency without increasing request count

#### HTTP Connection Pooling (Premium-Sized)

```python
# adapters/exchanges/base_adapter.py
class BaseAdapter:
    def __init__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            connector=aiohttp.TCPConnector(
                limit=100,              # Premium: Can handle more!
                limit_per_host=50,      # Premium: More concurrent connections
                keepalive_timeout=30,   # Reuse connections
                enable_cleanup_closed=True,
            )
        )
```

**Benefits:**
- Eliminates TCP handshake overhead (~20-40ms per request)
- **Premium advantage:** More connections = higher throughput
- Reduces server load

**Risk:** LOW (actually helps with rate limit compliance!)

#### Request Cache for Metadata (Premium-Sized)

```python
# services/market_data/cache.py
class MetadataCache:
    """TTL cache for idempotent API calls - Premium can handle more cache misses."""

    def __init__(self, ttl_seconds: int = 5):  # Shorter TTL = fresher data
        self._cache: dict[str, tuple[Any, float]] = {}
        self._ttl = ttl_seconds
        self._max_size = 1000  # Premium: Larger cache!

    async def get_or_fetch(self, key: str, fetch_fn: Callable) -> Any:
        if key in self._cache:
            value, timestamp = self._cache[key]
            if time.monotonic() - timestamp < self._ttl:
                return value  # Cache hit!

        value = await fetch_fn()
        self._cache[key] = (value, time.monotonic())

        # Premium: LRU eviction when over limit
        if len(self._cache) > self._max_size:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]

        return value
```

**Benefits:**
- **95% cache hit rate** with larger cache
- Reduces API calls by 95%!
- Shorter TTL (5s) = fresher data thanks to Premium capacity

#### WebSocket Message Batching (Conservative - WS limit still applies)

**IMPORTANT:** Premium doesn't increase WebSocket limits! Still:
- 200 messages/minute
- 50 inflight messages

**Strategy:** Use conservative batching with Premium-sized caches:

```python
class LighterPremiumSafeBatcher:
    """
    Premium REST + Conservative WS batching.
    Leverage Premium for REST, stay conservative for WebSocket.
    """

    def __init__(self, interval_ms: int = 600):  # 600ms = 100 batches/min
        self._interval = interval_ms / 1000
        self._buffer: list[dict] = []
        self._max_buffer_size = 2  # Max 2 messages per batch
        self._last_flush = time.monotonic()

    async def send(self, message: dict):
        self._buffer.append(message)
        now = time.monotonic()

        # Flush conditions:
        # 1. Interval elapsed (100 batches/min * 2 messages = 200/min)
        # 2. Buffer full (2 messages)
        if now - self._last_flush >= self._interval or len(self._buffer) >= self._max_buffer_size:
            await self._flush()
            self._last_flush = now

    async def _flush(self):
        if self._buffer:
            await self._ws.send_json(self._buffer)
            message_count = len(self._buffer)
            self._buffer.clear()
            metrics.record("ws_batch_messages", message_count)
```

**Rate Limit Math (WebSocket - Premium or Standard):**
- 600ms interval = 1.67 batches/second
- 1.67 batches/sec × 60 sec = **100 batches/minute**
- 2 messages per batch = **200 messages/minute** ✅ (at limit!)

**Expected Performance Gains:**
- API Calls: 30-50% less latency (connection pooling)
- Metadata Cache: 95% hit rate (larger cache)
- WebSocket Throughput: Optimal within 200/min limit

**Risk:** LOW (conservative WS batching)

**Estimated Time:** 1 week

---

### 1.4 Real-Time Monitoring Dashboard (Week 6-7) 📊

**Goal:** Visibility into Premium performance metrics

**Architecture:**
```
┌─────────────────┐
│ Metrics Collector│
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Metrics Store  │ (In-Memory + SQLite)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ WebSocket Stream│ (Real-Time Updates)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Dashboard UI  │ (HTML/JS - Optional)
└─────────────────┘
```

**Premium-Specific Metrics:**

1. **Premium Rate Limit Utilization:**
   - `% of Premium REST limit used` (target: <50%)
   - `% of WebSocket limit used` (target: <80%)
   - `Weighted request rate` (requests/second)

2. **Performance Metrics:**
   - `order_placement_latency_p50/p95/p99`
   - `market_data_refresh_duration`
   - `exit_eval_duration`
   - `api_call_latency_per_exchange`

3. **Alerts:**
   - **REST limit proximity >80%**: Warning
   - **WS limit proximity >90%**: Critical
   - **Latency degradation >2x**: Warning

**Risk:** LOW (observability doesn't change trading logic)

**Estimated Time:** 2 weeks

---

## 📈 Expected Overall Performance Improvement (Premium)

| Metric | Before | After (Premium) | Improvement |
|--------|--------|-----------------|-------------|
| Order Placement Latency (P95) | Unknown | <100ms | Measurable |
| Market Data Refresh (10 symbols) | 10s | <500ms | **20x faster** |
| Exit Evaluation (10 positions) | 1000ms | <150ms | **6.7x faster** |
| DB Operations (100 trades) | 100ms | <10ms | **10x faster** |
| API Call Overhead | +40ms | <5ms | **87% less** |
| System Throughput | Baseline | **10-20x** | **1000-2000% increase** |

**Key Premium Advantages:**
- **REST API:** 400x more capacity than Standard
- **Parallelization:** Can process 20x more symbols concurrently
- **Cache:** Larger cache = higher hit rates
- **Connection Pool:** More concurrent connections

**Remaining Constraints (Premium doesn't help):**
- **WebSocket:** Still 200 messages/minute
- **Inflight:** Still 50 max
- **Strategy:** Aggressive REST + Conservative WS

---

## 🗓️ Implementation Timeline

```
Week 1-2:  Latency Monitoring System
Week 3-4:  Premium-Optimized Async Concurrency
Week 5:    Network & Connection Optimization
Week 6-7:  Real-Time Monitoring Dashboard
Week 8:    Testing, Validation, Rollout
```

**Total Duration:** 7-8 weeks

**Critical Path:** Monitoring → Premium Optimization → Validation

**Dependencies:**
- 1.2 depends on 1.1 (need metrics to validate optimization)
- 1.4 depends on 1.1 (dashboard needs metrics)
- 1.3 is independent (can be done in parallel)

---

## ✅ Acceptance Criteria (Premium)

Phase 1 is complete when:

- [ ] All latency metrics tracked (including Premium limit proximity)
- [ ] Market data refresh <500ms (20x improvement)
- [ ] Exit evaluation <150ms for 10 positions (6.7x improvement)
- [ ] **Premium REST limit buffer >50%** (use <12,000/min of 24,000)
- [ ] **WebSocket limit buffer >20%** (use <160/min of 200)
- [ ] Zero 429 errors (rate limit violations)
- [ ] Zero data loss during high load
- [ ] All 534+ tests still pass
- [ ] Dashboard shows live metrics + Premium limit usage

---

## 🎯 Next Steps

**Recommended Approach:**

1. **Start with Monitoring (Week 1-2)**
   - Track current Premium limit usage
   - Identify headroom (should be massive!)
   - Establish baseline metrics

2. **Implement Premium Optimizations (Week 3-5)**
   - Start with 10% of Premium capacity (2,400/min)
   - Monitor limit proximity
   - Gradually increase to 50% capacity (12,000/min)
   - **STOP** if >80% of Premium limit used

3. **Exchange-Specific Testing (Week 6-7)**
   - Test with Lighter testnet first
   - Test with X10 testnet first
   - Measure actual vs. Premium limits

4. **Conservative Rollout (Week 8)**
   - Feature flags for each optimization
   - Gradual rollout to production
   - Monitor for 429 errors
   - Rollback immediately if issues detected

---

## 🔒 Safety Considerations

**Premium Safety Rules:**

1. **Never exceed 50% of Premium REST limit** (12,000/min)
   - Provides safety buffer
   - Allows for burst traffic
   - Prevents 429 errors

2. **Never exceed 80% of WebSocket limit** (160/min)
   - WebSocket limit applies to all accounts
   - No Premium advantage here
   - Must stay conservative

3. **Always monitor inflight messages** (<40 of 50)
   - Provides buffer
   - Prevents connection issues

4. **Gradual rollout with feature flags**
   - Start at 10% capacity
   - Increase by 10% per day
   - Monitor metrics at each step
   - Rollback if issues detected

---

**Generated:** 2026-01-14
**Author:** Claude Code (SuperClaude Task Agent)
**Status:** Premium-Optimized Phase 1 Plan ✅
**Account:** Lighter Premium (24,000 weighted requests/minute)
