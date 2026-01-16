# Session Snapshot 2026-01-14: Import Guard Verification Complete

## Previous Phase Completed ✅

### Verification Results
- **Import Guards**: All SDK imports properly guarded with try/except ImportError
- **Offline Tests**: 217/217 unit tests pass without Lighter SDK installed
- **Funding Safety**: Hourly canonical enforced for live trading (interval_hours=1)
- **Documentation**: Comprehensive verification documented in `claudedocs/offline_first_import_guard_verification_20260114.md`

### Key Files Verified
1. `src/funding_bot/adapters/exchanges/lighter/adapter.py:758-767` - Guarded ApiException import
2. `src/funding_bot/adapters/exchanges/lighter/ws_client.py:22-28` - Optional Configuration import
3. `src/funding_bot/config/settings.py:78-90` - Hourly funding validation
4. All 217 offline unit tests passing

## Next Phase: Bot-Behavior/PnL Audit

### Audit Scope
A) Entry/Rotation Logic
- Net-edge calculation (gross funding spread → net after fees/slippage)
- Thresholds, cooldowns, funding-time alignment

B) Execution Model
- Order types (maker/taker), rechase/grid, partial fills
- Idempotency (clientOrderId), race conditions, retries/backoff

C) Exit/Close Engine
- Chase/grid correctness, reduce-only flags, hedged unwinds

D) PnL/Accounting/Reconcile
- Funding vs fees vs trading PnL separation
- Double-count prevention, mark/index mismatch handling

E) Risk Controls
- Max leverage, max notional, liquidation buffer, kill-switch
- Stale data detection

### Exchange Research Required
- Lighter: https://docs.lighter.xyz/ + https://apidocs.lighter.xyz/
- Extended/X10: https://docs.extended.exchange/ + https://api.docs.extended.exchange/

Research topics:
- Fee schedules (maker/taker)
- Order constraints (min size, tick/step)
- Reduce-only semantics, post-only behavior
- Funding settlement timestamps
- Funding fee realization/query methods

## Test Policy
- **Default**: `pytest -q` runs offline (no DNS, no exchange SDK required)
- **Integration**: Marked with `@pytest.mark.integration`, skipped unless env vars present
- **Offline-first**: All new tests must pass without exchange SDKs installed