# CLAUDE.md — Funding Arb Bot (Project Instructions)

> **SuperClaude Integration**: This project uses the SuperClaude documentation structure.
> Claude Code automatically reads PLANNING.md, TASK.md, KNOWLEDGE.md, and CONTRIBUTING.md at session start.

---

## Quick Start

This is a **production-grade funding arbitrage bot** (Lighter Premium + X10/Extended).

**Core Philosophy**: Prioritize correctness (PnL/accounting), safety (risk controls), and cost-aware profit.

### Installation

```bash
python -m venv venv
venv\Scripts\activate
pip install -e ".[dev]"
```

### Run Tests

```bash
# Unit tests (offline, no SDK required)
pytest tests/unit/ -q

# Full test suite
pytest -q
```

---

## Documentation Structure

This project uses the **SuperClaude Framework** documentation structure for optimal Claude Code integration:

| Document | Purpose | When to Read |
|----------|---------|--------------|
| **[PLANNING.md](PLANNING.md)** | Architecture, design principles, absolute rules | Session start, before implementation |
| **[TASK.md](TASK.md)** | Current tasks, priorities, backlog | Daily, before starting work |
| **[KNOWLEDGE.md](KNOWLEDGE.md)** | Accumulated insights, best practices, troubleshooting | When encountering issues, learning patterns |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | Contribution guidelines, workflow | Before submitting PRs |

**Claude Code reads these files automatically at session start** for full context.

---

## SuperClaude Workflow

For any work, follow this sequence:

```bash
/sc:load        # Load project context and memories
/sc:analyze     # Analyze exact code paths + failure modes
/sc:task        # Break down tasks with acceptance criteria
/sc:implement   # Implement only planned tasks
/sc:test        # Run tests (offline/deterministic)
/sc:cleanup     # Refactor only if needed
/sc:document    # Update docs if behavior/config changes
/sc:save        # Save session context and memories
```

**Stop if**:
- Tests are failing with no clear next step
- Change requires guessing unknown API behavior
- Task scope balloons beyond stated acceptance criteria

---

## Non-Negotiables (Quick Reference)

1. **Security**: Never commit secrets (API keys, private keys, tokens)
2. **Live Trading**: Paper/sim is default unless `live_trading=true`
3. **Exit Semantics**: Preserve first-hit-wins (exactly one decision per tick)
4. **Exchange Fields**: Treat `filled_qty`, `fee` as **cumulative** unless proven otherwise
5. **Testing**: Any change to execution/PnL/accounting MUST include tests or test plan

**Full rules**: See [PLANNING.md](PLANNING.md) → Absolute Rules

---

## Key Technical Points

### PnL & Accounting
- Always compute exit price using **VWAP**: `sum(delta_qty * fill_price) / sum(delta_qty)`
- If exchange reports cumulative fields, compute **deltas per order_id**
- Post-close **readback** is source of truth if discrepancy > 3 bps or $0.30

### Funding Rate Cadence
- **Both exchanges apply funding HOURLY** (every hour on the hour)
- **Always keep `funding_rate_interval_hours = 1`** (never 8!)
- Setting `interval_hours = 8` causes **8x EV/APY errors**

### Lighter account_index
- `account_index = 0`: Explicitly use account 0
- `account_index = None`: Use adapter's default behavior
- **Never** treat `0` and `None` as the same

**Full details**: See [KNOWLEDGE.md](KNOWLEDGE.md) → PnL & Accounting Deep Dive

---

## Testing Requirements

- **Unit tests** must run offline (no DNS, no exchange SDK required)
- **Integration tests** must be marked `@pytest.mark.integration`
- Use try/except ImportError pattern for optional SDK imports

**Full testing policy**: See [CONTRIBUTING.md](CONTRIBUTING.md) → Testing Guidelines

---

## Current Status

### Phase 1: Performance & Resilience ✅ COMPLETE

**Premium-Optimized** (Lighter Premium: 24,000 req/min):

| Feature | Improvement | Status |
|---------|-------------|--------|
| Market Data Refresh | 20x faster (10s → 500ms) | ✅ |
| Exit Evaluation | 6.7x faster (1000ms → 150ms) | ✅ |
| DB Batch Operations | 10x faster (100ms → 10ms) | ✅ |
| Connection Pooling | 200 total, 100 per-host | ✅ |
| DNS Caching | 30 minutes TTL | ✅ |
| Keep-Alive | 5 minutes | ✅ |
| Market Cache | 1 hour TTL | ✅ |

**Test Results**: 273 unit tests passing, 3 skipped (to be updated)

**Full task list**: See [TASK.md](TASK.md)

---

## When Uncertain

If unsure about exchange semantics:
- Prefer conservative assumptions (taker cost, cumulative fields)
- Add a reconciliation warning
- Document assumptions in `docs/VENUE_CONTRACT.md`

---

## Repo Hygiene

**Do not commit**:
- `logs/`, `*.log`
- `*.db`, `*.sqlite*`
- `*.zip` artifacts
- `.pytest_cache/`, coverage artifacts
- `.env*`, secrets files

---

## Getting Started

1. **New to the project?** Read [PLANNING.md](PLANNING.md) first
2. **Ready to work?** Check [TASK.md](TASK.md) for current tasks
3. **Encountering issues?** See [KNOWLEDGE.md](KNOWLEDGE.md) for troubleshooting
4. **Contributing?** Follow [CONTRIBUTING.md](CONTRIBUTING.md) workflow

---

## SuperClaude Commands Reference

Useful commands for daily work:

```bash
/sc:load         # Load project context
/sc:analyze      # Analyze codebase
/sc:task         # Task breakdown
/sc:recommend     # Get command recommendations
/sc:help         # List all commands
```

---

## Project Stats

- **Language**: Python 3.14+
- **Exchanges**: Lighter (Premium), X10/Extended
- **Tests**: 273 unit tests passing
- **Documentation**: SuperClaude structure (4 files)
- **Status**: Production-ready for Premium accounts

---

**Last Updated**: 2026-01-14 (Phase 1 Complete)

**For detailed information, see the respective documentation files linked above.**
