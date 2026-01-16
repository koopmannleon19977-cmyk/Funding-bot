# CONTRIBUTING.md - Workflow & Guidelines

**This document is read automatically by Claude Code at session start.**

> **Purpose**: Contribution guidelines, development workflow, PR process.
> **When to Read**: Before submitting PRs, starting new work.
> **When to Update**: Workflow changes, new requirements.

---

## Development Workflow

### Using SuperClaude (Recommended)

For any work, follow this workflow:

1. **`/sc:load`** - Load project context and memories
2. **`/sc:analyze`** - Analyze exact code paths and failure modes
3. **`/sc:task`** - Break down tasks with acceptance criteria
4. **`/sc:implement`** - Implement only planned tasks
5. **`/sc:test`** - Run tests (offline/deterministic by default)
6. **`/sc:cleanup`** - Refactor only if needed
7. **`/sc:document`** - Update docs if behavior/config changes
8. **`/sc:save`** - Save session context and memories

### Stop Conditions

**Stop if**:
- Tests are failing and no clear next step exists
- A change would require guessing unknown API behavior
- The task scope balloons beyond stated acceptance criteria
- You encounter something that violates PLANNING.md principles

### Autonomy

Follow **PERMISSIONS.md** for approval decisions - only ask for exceptions listed there.

---

## Required Workflow (Step-by-Step)

### 1. Planning Phase

**Before writing any code:**

```bash
# Load project context
/sc:load

# Analyze the codebase
/sc:analyze [feature/bug area]

# Create task breakdown
/sc:task "[description]"
```

**Deliverables**:
- Clear understanding of what needs to change
- List of files to modify
- Test plan or verification approach
- Risk assessment (especially for PnL/accounting changes)

### 2. Implementation Phase

**Write code following these rules:**

1. **Read files first** - Never modify code you haven't read
2. **Use TodoWrite** - Track progress with clear task list
3. **Write tests first** (TDD when practical)
4. **Follow existing patterns** - Code style, naming, structure
5. **Add comments** - Only where logic isn't self-evident

**Quality Checks**:
- [ ] No security vulnerabilities (XSS, SQL injection, etc.)
- [ ] No breaking changes without migration plan
- [ ] Tests cover new functionality
- [ ] Documentation updated (if behavior changed)

### 3. Testing Phase

**Run full test suite:**

```bash
# Unit tests (must pass offline)
pytest tests/unit/ -q

# Integration tests (if applicable)
pytest tests/integration/ -v

# Verification tests (for critical features)
pytest tests/verification/ -v
```

**Acceptance Criteria**:
- ✅ All unit tests pass
- ✅ No new warnings (unless explained)
- ✅ Coverage maintained or improved
- ✅ Manual testing complete (for UI/interactive features)

### 4. Documentation Phase

**Update documentation if:**

- Behavior changed → Update CLAUDE.md, docs/
- Config added → Update settings.py, docs/, tests
- API changed → Update API_DOCUMENTATION.md
- Bug fixed → Update KNOWLEDGE.md (troubleshooting section)

**Run**:
```bash
/sc:document "[what changed]"
```

### 5. Commit Phase

**Follow conventional commit format:**

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `refactor`: Code refactoring (no behavior change)
- `perf`: Performance improvement
- `test`: Adding/updating tests
- `docs`: Documentation only
- `chore`: Maintenance tasks
- ` revert`: Revert previous commit

**Examples**:
```
feat(positions): add parallel exit evaluation for Premium accounts

Implements Phase 1.2 optimization:
- Parallel exit evaluation (10 concurrent positions)
- 6.7x faster than sequential (150ms vs 1000ms for 10 positions)
- Uses semaphore for rate limit compliance

Closes #123
```

```
fix(adapter): correct funding rate normalization for hourly cadence

The /8 in formulas is for realization, NOT payment cadence.
Setting interval_hours=8 causes 8x EV/APY errors.

Fixes #456
```

---

## Branch Strategy

### Feature Branch Workflow

```bash
# Create feature branch
git checkout -b feat/feature-name

# Make changes
git add .
git commit -m "feat(scope): description"

# Push to remote
git push origin feat/feature-name

# Create PR
gh pr create --title "feat(scope): description" --body "..."
```

### Branch Naming

- `feat/` - New features
- `fix/` - Bug fixes
- `refactor/` - Code refactoring
- `perf/` - Performance improvements
- `test/` - Test additions/updates
- `docs/` - Documentation only
- `chore/` - Maintenance tasks

### Commit Frequency

- **Small, frequent commits** preferred over large batches
- **Atomic commits** - One logical change per commit
- **Test-driven** - Commit test first, then implementation

---

## Pull Request Guidelines

### PR Template

```markdown
## Summary
[Brief description of changes]

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Performance improvement
- [ ] Code refactoring
- [ ] Documentation
- [ ] Other (please describe)

## Testing
- [ ] Unit tests added/updated
- [ ] All tests pass locally
- [ ] Integration tests pass (if applicable)
- [ ] Manual testing complete (if applicable)

## Checklist
- [ ] Code follows project style guidelines
- [ ] Self-review completed
- [ ] Comments added to complex code
- [ ] Documentation updated
- [ ] No new warnings generated
- [ ] CHANGELOG.md updated (if applicable)

## Related Issues
Closes #123
Related to #456
```

### PR Review Process

1. **Self-Review** - Review your own PR first
2. **Automated Checks** - CI must pass
3. **Peer Review** - At least one approval required
4. **Address Feedback** - Make requested changes
5. **Approval & Merge** - Maintainer approves and merges

### Review Criteria

**Code Quality**:
- Follows existing patterns and style
- No security vulnerabilities
- Proper error handling
- Adequate test coverage

**Correctness**:
- Logic is sound and well-reasoned
- Edge cases considered
- PnL/accounting preserved (for money-related changes)

**Documentation**:
- Clear commit messages
- Updated docs where applicable
- Comments on complex logic

---

## Testing Guidelines

### Test Organization

| Test Type | Location | Requirements | When to Run |
|-----------|----------|--------------|-------------|
| **Unit** | `tests/unit/` | No external dependencies | Always (CI/CD) |
| **Integration** | `tests/integration/` | API keys, env vars | Manual/CI |
| **Verification** | `tests/verification/` | Full setup | Pre-release |

### Unit Test Requirements

**Must**:
- Pass offline (no DNS, no exchange SDK)
- Use mocks for external dependencies
- Test edge cases and error conditions
- Be deterministic (same results every run)

**Example**:
```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.unit
async def test_exit_evaluation_with_negative_funding():
    # Arrange
    trade = _make_test_trade()
    trade.entry_apy = Decimal("0.40")
    mock_engine = AsyncMock()
    mock_engine.get_best_opportunity.return_value = None

    # Act
    decision = await _evaluate_exit(trade, Decimal("0"))

    # Assert
    assert decision.should_exit is True
    assert "funding" in decision.reason.lower()
```

### Integration Test Requirements

**Must**:
- Be marked `@pytest.mark.integration`
- Skip without required env vars
- Clean up resources (close connections, etc.)
- Be idempotent (can run multiple times safely)

**Example**:
```python
import pytest
import os

@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("LIGHTER_API_KEY"), reason="No API key")
async def test_live_market_data_fetch():
    # Arrange
    adapter = await create_adapter(settings)

    # Act
    markets = await adapter.load_markets()

    # Assert
    assert len(markets) > 0
    assert "BTC-PERP" in markets
```

### Verification Test Requirements

**Purpose**: End-to-end scenario testing

**Must**:
- Test complete user workflows
- Use real or realistic data
- Verify business logic end-to-end

**Example Scenarios**:
- Full position lifecycle (open → monitor → close)
- Rebalance during drift
- Emergency close on broken hedge
- Funding rate flip triggers exit

---

## Code Review Guidelines

### For Reviewers

**What to Check**:
1. **Correctness** - Does it do what it claims?
2. **Safety** - Any security or PnL risks?
3. **Testing** - Are tests adequate?
4. **Documentation** - Is it clear and complete?
5. **Style** - Does it match project patterns?

**How to Review**:
1. Read PR description first
2. Check each file change
3. Run tests locally if possible
4. Ask questions if unclear
5. Approve or request changes

### For Authors

**How to Respond**:
1. Address all feedback
2. Explain your reasoning if you disagree
3. Update tests if needed
4. Request re-review when ready

---

## Troubleshooting

### "Tests failing with SDK not found"

**Cause**: Test imports exchange SDK without guard

**Solution**: Use try/except ImportError pattern (see KNOWLEDGE.md)

### "ImportError: cannot import name X"

**Cause**: Circular import or missing dependency

**Solution**:
- Check for circular imports
- Run `pip install -e ".[dev]"`
- Check pyproject.toml dependencies

### "pytest: command not found"

**Cause**: pytest not installed or not in PATH

**Solution**:
```bash
# Ensure venv is activated
venv\Scripts\activate

# Install dev dependencies
pip install -e ".[dev]"
```

---

## Getting Help

### Resources

1. **PLANNING.md** - Architecture and design principles
2. **KNOWLEDGE.md** - Technical knowledge and troubleshooting
3. **TASK.md** - Current tasks and backlog
4. **CLAUDE.md** - Project instructions

### SuperClaude Commands

- `/sc:help` - List all available commands
- `/sc:recommend` - Get command recommendations
- `/sc:analyze` - Analyze codebase
- `/sc:task` - Task breakdown and planning

### Asking Questions

When asking questions:
1. Check docs first (PLANNING.md, KNOWLEDGE.md)
2. Search existing issues and PRs
3. Provide context (error, steps to reproduce)
4. Include relevant logs/output

---

## Recognition

Thanks for contributing! Every contribution helps make this bot better.

**Contributors**:
- Your name here when you contribute

---

## See Also

- **PLANNING.md**: Architecture, design principles, absolute rules
- **TASK.md**: Current tasks, priorities, backlog
- **KNOWLEDGE.md**: Technical knowledge, best practices, troubleshooting
