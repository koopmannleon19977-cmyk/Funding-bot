# Serena Memory System

**This directory is used by Serena MCP for session persistence and learning.**

> **Purpose**: Store learned patterns, solutions, and workflow metrics across sessions.
> **Auto-Managed**: Files are automatically read/written by `/sc:save` and `/sc:load`.

---

## File Structure

| File | Purpose | Managed By |
|------|---------|------------|
| `patterns_learned.jsonl` | Learned code patterns (auto-generated) | `/sc:save` |
| `solutions_learned.jsonl` | Solutions to problems (auto-generated) | `/sc:save` |
| `workflow_metrics.jsonl` | Performance metrics (auto-generated) | `/sc:save` |
| `reflexion.jsonl` | Error learning and improvements (manual) | `/sc:save` |
| `pm_context.md` | PM-Agent context for project management | `/sc:pm` |
| `WORKFLOW_METRICS_SCHEMA.md` | Schema definition for metrics | Reference |

---

## How It Works

### Automatic Learning

When you use `/sc:save`, Serena automatically:
1. Analyzes the session for learned patterns
2. Extracts solutions to problems encountered
3. Records workflow metrics
4. Writes to `*_learned.jsonl` files

### Session Persistence

When you use `/sc:load`, Serena:
1. Reads all `.jsonl` files
2. Loads learned patterns into context
3. Applies solutions from previous sessions
4. Provides continuity across sessions

### Manual Reflexion

`reflexion.jsonl` is for manual error learning:
```jsonl
{"pattern": "Forgot to invalidate cache after trade update", "solution": "Call store._invalidate_open_trades_cache()", "impact": "high"}
{"pattern": "Test mock setup incompatible with parallel implementation", "solution": "Use direct async functions instead of MagicMock", "impact": "medium"}
```

---

## File Formats

### patterns_learned.jsonl

```jsonl
{"pattern": "Use semaphore for controlled concurrency", "context": "Premium optimization allows 10 concurrent exit evaluations", "tags": ["concurrency", "performance"]}
{"pattern": "Always compute delta_qty from cumulative fields", "context": "Exchange reports filled_qty cumulatively", "tags": ["accounting", "pnl"]}
```

### solutions_learned.jsonl

```jsonl
{"problem": "Tests failing with 'SDK not found'", "solution": "Use try/except ImportError pattern with fallback classes", "file": "ws_client.py", "impact": "high"}
{"problem": "Ghost positions on startup", "solution": "Flush write queue before shutdown in store.close()", "file": "store.py", "impact": "critical"}
```

### workflow_metrics.jsonl

```jsonl
{"timestamp": "2026-01-14T10:00:00Z", "operation": "market_data_refresh", "duration_ms": 450, "success": true}
{"timestamp": "2026-01-14T10:01:00Z", "operation": "exit_evaluation", "duration_ms": 150, "success": true}
```

---

## Best Practices

### When to Use `/sc:save`

- After completing a feature or bug fix
- After learning a new pattern or solution
- After significant refactoring
- Before ending a work session

### When to Use `/sc:load`

- At the start of a new session
- When switching contexts or projects
- When you need previous session context

### Manual Reflexion

Add to `reflexion.jsonl` when:
- You encounter a recurring error
- You discover a better way to do something
- You want to remember a pitfall to avoid

---

## Schema Reference

See `WORKFLOW_METRICS_SCHEMA.md` for the complete schema definition.

---

## See Also

- **PLANNING.md**: Architecture and design principles
- **TASK.md**: Current tasks and backlog
- **KNOWLEDGE.md**: Technical knowledge and troubleshooting
- **CONTRIBUTING.md**: Workflow guidelines
