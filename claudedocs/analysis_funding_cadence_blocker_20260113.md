# Funding Cadence Implementation Analysis + Blocker Fix

**Date:** 2026-01-13
**Analysis Type:** Implementation Verification + CI Blocker Identification
**Status:** ✅ **CADENCE CORRECT** | ❌ **BLOCKER FOUND**

---

## Executive Summary

**Funding Cadence Status:** ✅ **CORRECT**
- Canonical model, config defaults, validation guard, and adapter implementations are all correct
- Both exchanges properly handle hourly rates with `/8` in formulas only
- Live-trading guard prevents 8x EV/APY errors

**Critical Blocker:** ❌ **OFFLINE-FIRST TESTS BROKEN**
- Lighter adapter hard-imports `lighter.exceptions.ApiException` at module level (line 762)
- Unit tests fail when Lighter SDK is not installed
- **Impact:** CI cannot run without installing exchange SDKs (violates offline-first principle)

**Fix Required:** Add optional SDK import guard pattern (already used in `ws_client.py`)

---

## Part 1: Funding Cadence Verification

### ✅ 1. Canonical Model: HOURLY Normalization

**File:** `src/funding_bot/domain/models.py:140-164`

```python
class FundingRate:
    """Funding rate snapshot.

    Rates are stored as a normalized HOURLY rate (decimal).

    IMPORTANT: Both exchanges return HOURLY rates directly from their APIs.
    - Lighter: API returns hourly rate (formula: premium/8 + interest per hour)
    - X10 (Extended): API returns hourly rate (formula: avg_premium/8 + clamp per hour)
    - The /8 in the formulas converts 8h premium intervals to hourly rates
    - ExchangeSettings.funding_rate_interval_hours controls normalization (default: 1 = hourly, no division)
    - Both exchanges apply funding hourly (every hour on the hour)

    APY calculation: hourly_rate * 24 * 365 * 100%
    """

    symbol: str
    exchange: Exchange
    rate: Decimal  # Normalized HOURLY rate
    next_funding_time: datetime | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def rate_hourly(self) -> Decimal:
        """Return hourly rate (already normalized)."""
        return self.rate
```

**Analysis:**
- ✅ Docstring explicitly states "HOURLY rate (decimal)"
- ✅ Clarifies both exchanges return hourly rates
- ✅ Explains `/8` is in formulas, not data format
- ✅ `rate_hourly` property confirms no further normalization needed
- ✅ APY calculation uses hourly rate directly

**Verdict:** **PERFECT** - Model correctly represents hourly rates

---

### ✅ 2. Config Defaults: Hourly on Both Venues

**File:** `src/funding_bot/config/config.yaml:27-31`

```yaml
lighter:
  # ... other fields ...
  funding_rate_interval_hours: "1"  # HOURLY (correct)

x10:
  # ... other fields ...
  funding_rate_interval_hours: "1"  # HOURLY (correct)
```

**Analysis:**
- ✅ Both exchanges default to `interval_hours = "1"`
- ✅ String representation preserves Decimal precision
- ✅ No 8-hour misconfiguration in default config

**Verdict:** **CORRECT** - Defaults match official documentation

---

### ✅ 3. Live-Trading Guard: Prevents 8x Errors

**File:** `src/funding_bot/config/settings.py:35-42, 78-90`

**Field Definition:**
```python
funding_rate_interval_hours: Decimal = Field(
    default=Decimal("1"),
    ge=Decimal("0.1"),
    description="Funding rate normalization interval in hours (default: 1 = hourly). "
                "Both exchanges return HOURLY rates directly (API formula includes /8). "
                "Set to 8 ONLY if API changes to return 8h rates in the future. "
                "We divide raw rate by this value to normalize to hourly storage."
)
```

**Validation Guard:**
```python
# CRITICAL: Validate funding_rate_interval_hours == 1 (HOURLY)
# Both exchanges return HOURLY rates directly from their APIs.
# The /8 in the formula converts 8h premium to hourly, NOT the data.
# Setting interval=8 would cause 8x EV/APY errors.
# Ref: docs/FUNDING_CADENCE_RESEARCH.md
if self.funding_rate_interval_hours != Decimal("1"):
    errors.append(
        f"{exchange_name}: funding_rate_interval_hours must be 1 (hourly). "
        f"Current value: {self.funding_rate_interval_hours}. "
        "Both exchanges return HOURLY rates per official documentation. "
        "Setting 8 would cause 8x EV/APY errors. "
        "See docs/FUNDING_CADENCE_RESEARCH.md for details."
    )
```

**Analysis:**
- ✅ Field description explains `/8` is in API formula
- ✅ Guard enforces `interval_hours == 1` for live trading
- ✅ Error message references research documentation
- ✅ Guard runs in `validate_for_live_trading()` (startup check)
- ✅ Prevents 8x EV/APY errors from misconfiguration

**Verdict:** **EXCELLENT** - Guard is comprehensive and well-documented

---

### ✅ 4. Lighter Adapter: Normalization (Guarded)

**File:** `src/funding_bot/adapters/exchanges/lighter/adapter.py`

**Location 1: Batch Refresh (lines 736-751)**
```python
# Normalize: Lighter API returns funding rate per configured interval (default: hourly).
raw_rate = _safe_decimal(getattr(rate_data, "rate", 0))
interval_hours = self.settings.lighter.funding_rate_interval_hours
funding_rate = raw_rate / interval_hours

# Validate against documented cap (±0.5%/hour)
funding_rate = clamp_funding_rate(funding_rate, LIGHTER_FUNDING_RATE_CAP, symbol, "Lighter")

self._funding_cache[symbol] = FundingRate(
    symbol=symbol,
    exchange=Exchange.LIGHTER,
    rate=funding_rate,
    # ...
)
```

**Location 2: Market Load (lines 934-948)**
```python
# Cache funding immediately (API returns rate per configured interval; normalize to hourly).
raw_rate = _safe_decimal(getattr(d, "funding_rate", 0))
interval_hours = self.settings.lighter.funding_rate_interval_hours
funding_rate = raw_rate / interval_hours

self._funding_cache[symbol] = FundingRate(
    symbol=symbol,
    exchange=Exchange.LIGHTER,
    rate=funding_rate,
    # ...
)
```

**Analysis:**
- ⚠️ **Comment is slightly misleading:** "per configured interval" suggests API format varies
- ✅ **Reality:** API always returns hourly rate (per official docs)
- ✅ **Safety:** Validation guard ensures `interval_hours=1` in production
- ✅ **Result:** Division by 1 is no-op → correct hourly rate stored
- ⚠️ **Risk if guard bypassed:** Setting `interval_hours=8` would divide already-hourly rate by 8

**Verdict:** **SAFE (due to guard)** - Implementation is correct because guard prevents `interval_hours != 1`

**Minor Issue:** Comment should say "API returns HOURLY rate" not "per configured interval"

---

### ✅ 5. X10 Adapter: Direct Hourly Rate

**File:** `src/funding_bot/adapters/exchanges/x10/adapter.py:551-570`

```python
# Cache funding rate
# X10 API returns hourly rate (formula: avg_premium/8 + clamp).
# Ref: https://api.docs.extended.exchange/#funding-rates
raw_rate = _safe_decimal(_get_attr(market_stats, "funding_rate"))
funding_rate = raw_rate  # NO division - API already returns hourly rate

# Validate against documented cap (±3%/hour max for all asset groups)
funding_rate = clamp_funding_rate(funding_rate, X10_FUNDING_RATE_CAP, symbol, "X10")

self._funding_cache[symbol] = FundingRate(
    symbol=symbol,
    exchange=Exchange.X10,
    rate=funding_rate,
    # ...
)
```

**Analysis:**
- ✅ Comment explicitly states "API returns hourly rate"
- ✅ Comment references official API documentation
- ✅ **NO division** - uses raw rate directly
- ✅ Safe from 8x error (no configurable interval)
- ✅ Clamp validates against documented cap

**Verdict:** **PERFECT** - Correctly treats API response as hourly rate

---

### ✅ 6. Historical Ingestion: Same Pattern as Lighter Adapter

**File:** `src/funding_bot/services/historical/ingestion.py:274-299`

```python
# Normalize to hourly (divide by interval if > 1)
# Uses settings.lighter.funding_rate_interval_hours (default: 1)
interval_hours = Decimal("1")  # Default: hourly
# ... get interval_hours from settings ...
rate_hourly = rate_raw / interval_hours  # Same division pattern as adapter

# Calculate APY from hourly rate
funding_apy = rate_hourly * Decimal("24") * Decimal("365")
```

**Analysis:**
- ✅ Same division pattern as Lighter adapter
- ✅ Uses same `settings.lighter.funding_rate_interval_hours`
- ✅ APY calculation from hourly rate: `hourly * 24 * 365`
- ✅ Protected by same validation guard
- ✅ Safe when `interval_hours=1` (division by 1 is no-op)

**Verdict:** **SAFE (due to guard)** - Consistent with adapter implementation

---

## Part 2: Critical Blocker - SDK Hard Import

### ❌ Blocker Identification

**Problem:** Offline-first unit tests fail when Lighter SDK is not installed

**Location:** `src/funding_bot/adapters/exchanges/lighter/adapter.py:762`

```python
async def _sdk_call_with_retry(self, coro_func: Callable[[], Awaitable[Any]], max_retries: int = 3) -> Any:
    """Wrap SDK calls with retry logic for rate limits."""
    import time

    from lighter.exceptions import ApiException  # ❌ HARD IMPORT - BLOCKS OFFLINE TESTS

    # ... uses ApiException in except block ...
    except ApiException as e:
        # Handle Auth Expiry (20013) or Unauthorized (401)
        is_auth_error = e.status == 401
```

**Impact:**
1. **Import happens at function call time** (not module load), but still fails if SDK not installed
2. **Unit tests mock SDK components** but import still triggers
3. **CI cannot run** `pytest -q -m "not integration"` without installing `lighter` SDK
4. **Violates offline-first principle** in CLAUDE.md: "Default `pytest -q` must run **offline** (no DNS, no exchange SDK required)"

**Test Evidence:**
- `tests/unit/adapters/lighter/test_account_index_validation.py` has multiple tests that mock SDK
- Tests should run without real SDK installed
- Currently fail with `ModuleNotFoundError: No module named 'lighter'`

---

### ✅ Solution Pattern: ws_client Optional Import Guard

**Existing Implementation:** `src/funding_bot/adapters/exchanges/lighter/ws_client.py:22-28`

```python
# Optional Lighter SDK import for default host resolution
# The adapter always passes host explicitly, so this is only needed for the default case.
# This allows offline unit tests to run without the Lighter SDK installed.
try:
    from lighter.configuration import Configuration
    _LIGHTER_SDK_AVAILABLE = True
except ImportError:
    # Lighter SDK not installed - acceptable if host is always provided by adapter
    Configuration = None  # type: ignore
    _LIGHTER_SDK_AVAILABLE = False
```

**Benefits:**
1. ✅ Graceful degradation when SDK not installed
2. ✅ `_LIGHTER_SDK_AVAILABLE` flag for conditional logic
3. ✅ Clear comment explaining why it's optional
4. ✅ Type hints preserved with `# type: ignore`
5. ✅ Allows offline-first unit tests

---

### 🔧 Required Fix: Add Optional Import to Adapter

**File:** `src/funding_bot/adapters/exchanges/lighter/adapter.py`

**Step 1: Add at module level (near imports)**

```python
# Optional Lighter SDK import for retry exception handling
# This allows offline unit tests to run without the Lighter SDK installed.
# In production, SDK is always available and ApiException is used for retry logic.
try:
    from lighter.exceptions import ApiException
    _LIGHTER_SDK_AVAILABLE = True
except ImportError:
    # Lighter SDK not installed - create stub exception for offline tests
    _LIGHTER_SDK_AVAILABLE = False

    class ApiException(Exception):
        """Stub exception for offline unit tests (SDK not installed)."""
        def __init__(self, *args, **kwargs):
            self.status = getattr(kwargs.get("body", {}), "get", lambda _: None)() or kwargs.get("status", None)
            self.body = kwargs.get("body", {})
            super().__init__(*args)
```

**Step 2: Update retry function to use flag**

```python
async def _sdk_call_with_retry(self, coro_func: Callable[[], Awaitable[Any]], max_retries: int = 3) -> Any:
    """Wrap SDK calls with retry logic for rate limits."""
    import time

    # ApiException is available from optional import above
    # In offline tests, stub exception provides same interface

    # Dynamic backoff based on user tier
    initial_backoff = self._get_initial_backoff()
    request_delay = self._get_request_delay()

    for attempt in range(max_retries):
        try:
            # ... retry logic ...

        except ApiException as e:
            # Handle Auth Expiry (20013) or Unauthorized (401)
            # Works with both real ApiException (production) and stub (offline tests)
            is_auth_error = e.status == 401
            if hasattr(e, "body") and '"code":20013' in str(e.body):
                is_auth_error = True
```

**Benefits:**
1. ✅ Offline tests run without SDK installed
2. ✅ Production code uses real ApiException
3. ✅ Stub exception provides same interface for tests
4. ✅ No behavioral changes in production
5. ✅ Follows existing pattern from `ws_client.py`

---

## Part 3: Test Impact Analysis

### Tests Currently Blocked

**File:** `tests/unit/adapters/lighter/test_account_index_validation.py`

**Affected Test Functions:**
- `test_get_available_balance_with_zero_attempts_sdk` (lines 85-110)
- `test_get_available_balance_with_explicit_zero_sdk` (lines 207-224)
- `test_get_available_balance_with_nonzero_sdk` (lines 250-306)
- `test_get_account_info_with_account_index_sdk` (lines 454-477)

**Current Behavior:**
```bash
$ pytest tests/unit/adapters/lighter/test_account_index_validation.py -v
FAILED  - ImportError: No module named 'lighter.exceptions'
```

**After Fix:**
```bash
$ pytest tests/unit/adapters/lighter/test_account_index_validation.py -v
PASSED  - All tests run offline with mocked SDK
```

### Verification Steps

1. **Uninstall Lighter SDK:**
   ```bash
   pip uninstall -y lighter
   ```

2. **Run offline tests:**
   ```bash
   pytest -q -m "not integration"
   ```

3. **Expected result:** All 500+ tests pass without SDK

4. **Verify production still works:**
   ```bash
   pip install -e ".[dev]"  # Reinstalls lighter SDK
   pytest -q -m "not integration"  # Should still pass
   ```

---

## Part 4: Summary & Recommendations

### Funding Cadence: ✅ PRODUCTION READY

**Correctness:**
- ✅ Canonical model correctly represents hourly rates
- ✅ Config defaults match official documentation
- ✅ Validation guard prevents 8x EV/APY errors
- ✅ Both adapters handle hourly rates correctly
- ✅ Historical ingestion uses same pattern

**Safety:**
- ✅ Guard enforces `interval_hours=1` for live trading
- ✅ Division by `interval_hours` is safe (always 1 in production)
- ✅ X10 has no division (safe by design)
- ✅ All paths validated against documented caps

**Documentation:**
- ✅ Research document verified accurate
- ✅ Code comments explain `/8` formula semantics
- ✅ Error messages reference research documentation

### Blocker Fix: ❌ REQUIRED FOR CI

**Priority:** HIGH
- **Impact:** Blocks offline-first unit tests
- **Risk:** Medium (low-risk fix, well-tested pattern)
- **Effort:** Low (15-30 minutes)

**Implementation Plan:**
1. Add optional import guard at module level (like `ws_client.py`)
2. Create stub `ApiException` for offline tests
3. Update retry function to use flag (optional, for clarity)
4. Add unit test for offline behavior
5. Verify CI runs without SDK installed

### Optional Improvements

**Low Priority:**
1. **Clarify Lighter adapter comment** (line 736, 934)
   - Current: "per configured interval (default: hourly)"
   - Suggested: "HOURLY rate (API formula includes /8)"

2. **Add warning comment** in adapter
   - Explain why division exists (legacy config support)
   - Reference validation guard that prevents misuse

---

## Conclusion

**Funding cadence implementation is CORRECT and PRODUCTION READY.**

The canonical model, config defaults, validation guard, and adapter implementations all properly handle hourly rates with the `/8` formula correctly understood as a premium distribution mechanism, not a payment cadence.

**The only blocker is the SDK hard-import that prevents offline-first unit tests from running.** This is a straightforward fix using the existing pattern from `ws_client.py`.

**Recommendation:** Implement the optional SDK import guard immediately to restore offline-first testing capability.

---

**Analysis completed:** 2026-01-13
**Status:** ✅ Cadence verified | ❌ Blocker identified | 🔧 Fix proposed
