# Workflow Metrics Schema

**Schema for workflow metrics tracked by Serena MCP.**

> **Purpose**: Define structure for performance metrics and operations tracking.
> **Usage**: Reference for `workflow_metrics.jsonl` format.

---

## Schema Definition

### Common Fields (All Records)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `timestamp` | ISO 8601 datetime | ✅ | When the operation occurred |
| `session_id` | string | ✅ | Unique session identifier |
| `operation` | string | ✅ | Type of operation (see types below) |
| `success` | boolean | ✅ | Whether operation succeeded |

### Operation-Specific Fields

#### Market Data Refresh

```json
{
  "timestamp": "2026-01-14T10:00:00Z",
  "session_id": "session_123",
  "operation": "market_data_refresh",
  "success": true,
  "duration_ms": 450,
  "symbols_count": 10,
  "parallel": true,
  "concurrent": 20
}
```

| Field | Type | Description |
|-------|------|-------------|
| `duration_ms` | number | Operation duration in milliseconds |
| `symbols_count` | number | Number of symbols refreshed |
| `parallel` | boolean | Whether parallel execution was used |
| `concurrent` | number | Concurrent operations (if parallel) |

#### Exit Evaluation

```json
{
  "timestamp": "2026-01-14T10:01:00Z",
  "session_id": "session_123",
  "operation": "exit_evaluation",
  "success": true,
  "duration_ms": 150,
  "trades_count": 10,
  "exits_triggered": 2,
  "parallel": true,
  "concurrent": 10
}
```

| Field | Type | Description |
|-------|------|-------------|
| `duration_ms` | number | Operation duration in milliseconds |
| `trades_count` | number | Number of trades evaluated |
| `exits_triggered` | number | Number of exits triggered |
| `parallel` | boolean | Whether parallel execution was used |
| `concurrent` | number | Concurrent operations (if parallel) |

#### Database Operation

```json
{
  "timestamp": "2026-01-14T10:02:00Z",
  "session_id": "session_123",
  "operation": "database_write_batch",
  "success": true,
  "duration_ms": 10,
  "records_count": 100,
  "batch_size": 50
}
```

| Field | Type | Description |
|-------|------|-------------|
| `duration_ms` | number | Operation duration in milliseconds |
| `records_count` | number | Number of records written |
| `batch_size` | number | Batch size used |

#### Code Analysis

```json
{
  "timestamp": "2026-01-14T10:03:00Z",
  "session_id": "session_123",
  "operation": "code_analysis",
  "success": true,
  "duration_ms": 5000,
  "files_analyzed": 25,
  "tool": "/sc:analyze"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `duration_ms` | number | Operation duration in milliseconds |
| `files_analyzed` | number | Number of files analyzed |
| `tool` | string | Tool or command used |

#### Test Execution

```json
{
  "timestamp": "2026-01-14T10:04:00Z",
  "session_id": "session_123",
  "operation": "test_execution",
  "success": true,
  "duration_ms": 15000,
  "tests_run": 273,
  "tests_passed": 273,
  "tests_failed": 0,
  "tests_skipped": 3
}
```

| Field | Type | Description |
|-------|------|-------------|
| `duration_ms` | number | Operation duration in milliseconds |
| `tests_run` | number | Total number of tests run |
| `tests_passed` | number | Number of tests passed |
| `tests_failed` | number | Number of tests failed |
| `tests_skipped` | number | Number of tests skipped |

---

## Operation Types

| Operation | Description | tracked By |
|-----------|-------------|-----------|
| `market_data_refresh` | Market data refresh operation | refresh.py |
| `exit_evaluation` | Exit rule evaluation | manager.py |
| `database_write_batch` | Batch database write | write_queue.py |
| `code_analysis` | Codebase analysis | `/sc:analyze` |
| `test_execution` | Test suite execution | `/sc:test` |
| `task_breakdown` | Task breakdown | `/sc:task` |
| `implementation` | Code implementation | `/sc:implement` |
| `refactoring` | Code refactoring | `/sc:cleanup` |
| `documentation` | Documentation update | `/sc:document` |

---

## Performance Targets

Based on Premium optimization (Phase 1):

| Operation | Target | Actual |
|-----------|--------|--------|
| `market_data_refresh` (10 symbols) | ≤ 500ms | ~500ms ✅ |
| `exit_evaluation` (10 positions) | ≤ 150ms | ~150ms ✅ |
| `database_write_batch` (100 trades) | ≤ 10ms | ~10ms ✅ |

---

## Usage Examples

### Reading Metrics

```python
import json

# Read all metrics
with open('docs/memory/workflow_metrics.jsonl', 'r') as f:
    for line in f:
        metric = json.loads(line)
        print(f"{metric['operation']}: {metric['duration_ms']}ms")
```

### Analyzing Performance

```python
from collections import defaultdict
import json

# Calculate average duration per operation type
durations = defaultdict(list)

with open('docs/memory/workflow_metrics.jsonl', 'r') as f:
    for line in f:
        metric = json.loads(line)
        if metric.get('success'):
            durations[metric['operation']].append(metric['duration_ms'])

for op, values in durations.items():
    avg = sum(values) / len(values)
    print(f"{op}: {avg:.0f}ms average ({len(values)} samples)")
```

---

## See Also

- **README.md**: Memory system overview
- **PLANNING.md**: Performance targets
- **KNOWLEDGE.md**: Troubleshooting performance issues
