# Documentation Cleanup Plan - Safe Deletion Strategy

> **Generated**: 2026-01-17
> **Purpose**: Safe removal of outdated documentation files from funding-bot project
> **Status**: Planning Phase - No changes made yet

---

## Executive Summary

This plan provides a safe, structured approach to deleting 30 outdated documentation files while preserving all essential project documentation. The cleanup will reduce documentation clutter while maintaining project integrity and ensuring the bot continues to function correctly.

### Key Metrics

| Metric                          | Value                       |
| ------------------------------- | --------------------------- |
| **Files to Delete**             | 30                          |
| **Files to Keep**               | 15                          |
| **Files Requiring Review**      | 4 (all recommended to keep) |
| **Critical Dependencies Found** | 1 (docstring reference)     |
| **Estimated Risk**              | Low                         |
| **Backup Required**             | Yes                         |

---

## Files Analysis

### Files to Delete (30 files)

#### Category 1: ClaudeDocs Analysis Files (25 files)

These are temporary analysis files created during development sessions. They contain outdated research and implementation notes that have been superseded by the source of truth document.

| #   | File                                                             | Size  | Last Modified | Reason for Deletion                              |
| --- | ---------------------------------------------------------------- | ----- | ------------- | ------------------------------------------------ |
| 1   | `claudedocs/analysis_funding_cadence_blocker_20260113.md`        | ~5KB  | 2026-01-13    | Outdated analysis, superseded                    |
| 2   | `claudedocs/analyze_offline_first_import_guard_fix_20260114.md`  | ~3KB  | 2026-01-14    | Temporary fix documentation                      |
| 3   | `claudedocs/BACKILL_VERIFICATION_REPORT_20250115.md`             | ~8KB  | 2026-01-15    | Old verification report                          |
| 4   | `claudedocs/bot_behavior_verification_table_20260114.md`         | ~4KB  | 2026-01-14    | Outdated verification data                       |
| 5   | `claudedocs/EXCHANGE_RATE_LIMITS_RESEARCH_20260114.md`           | ~6KB  | 2026-01-14    | Research superseded by EXCHANGE_API_REFERENCE.md |
| 6   | `claudedocs/fix_offline_tests_sdk_import_guard_20260113.md`      | ~3KB  | 2026-01-13    | Fix implementation complete                      |
| 7   | `claudedocs/git_commit_summary_20260113.md`                      | ~2KB  | 2026-01-13    | Temporary commit notes                           |
| 8   | `claudedocs/git_commit_summary_20260114.md`                      | ~2KB  | 2026-01-14    | Temporary commit notes                           |
| 9   | `claudedocs/implement_import_guard_verification_20260114.md`     | ~4KB  | 2026-01-14    | Implementation complete                          |
| 10  | `claudedocs/offline_first_import_guard_verification_20260114.md` | ~3KB  | 2026-01-14    | Verification complete                            |
| 11  | `claudedocs/ondo_orderbook_fix_complete_20260115.md`             | ~7KB  | 2026-01-15    | Fix complete, superseded                         |
| 12  | `claudedocs/PHASE_1_PERFORMANCE_ANALYSIS_20260114.md`            | ~10KB | 2026-01-14    | Analysis complete                                |
| 13  | `claudedocs/PHASE_1_PREFLIGHT_LIQUIDITY_COMPLETE_20260115.md`    | ~8KB  | 2026-01-15    | Phase complete                                   |
| 14  | `claudedocs/PHASE_1_PREMIUM_OPTIMIZED_PLAN_20260114.md`          | ~6KB  | 2026-01-14    | Plan implemented                                 |
| 15  | `claudedocs/PHASE_COMPLETE_PREFLIGHT_LIQUIDITY_20260115.md`      | ~7KB  | 2026-01-15    | Phase complete                                   |
| 16  | `claudedocs/PLAN_ORDERBOOK_OPTIMIZATION_20260115.md`             | ~5KB  | 2026-01-15    | Plan implemented                                 |
| 17  | `claudedocs/quality_gate_reflection_20260113.md`                 | ~3KB  | 2026-01-13    | Reflection notes                                 |
| 18  | `claudedocs/quality_gate_reflection_20260114.md`                 | ~3KB  | 2026-01-14    | Reflection notes                                 |
| 19  | `claudedocs/research_funding_cadence_verification_20260113.md`   | ~5KB  | 2026-01-13    | Research superseded                              |
| 20  | `claudedocs/research_orderbook_depth_api_20260115.md`            | ~6KB  | 2026-01-15    | Research superseded                              |
| 21  | `claudedocs/research_x10_liquidity_integration_20260115.md`      | ~7KB  | 2026-01-15    | Integration complete                             |
| 22  | `claudedocs/test_report_offline_20260113.md`                     | ~4KB  | 2026-01-13    | Old test report                                  |
| 23  | `claudedocs/test_report_offline_20260114.md`                     | ~4KB  | 2026-01-14    | Old test report                                  |
| 24  | `claudedocs/troubleshooting_ondo_orderbook_issue_20260115.md`    | ~6KB  | 2026-01-15    | Issue resolved                                   |
| 25  | `claudedocs/x10_liquidity_fix_implementation_20260115.md`        | ~7KB  | 2026-01-15    | Fix implemented                                  |

#### Category 2: Docs/Memory Session Files (3 files)

| #   | File                                                     | Size | Last Modified | Reason for Deletion     |
| --- | -------------------------------------------------------- | ---- | ------------- | ----------------------- |
| 26  | `docs/memory/preflight_liquidity_implementation_plan.md` | ~5KB | 2026-01-15    | Implementation complete |
| 27  | `docs/memory/SESSION_SUMMARY_20260115.md`                | ~6KB | 2026-01-15    | Old session summary     |
| 28  | `docs/memory/SESSION_2026-01-15_PHASE2_REFACTORING.md`   | ~4KB | 2026-01-15    | Phase complete          |

#### Category 3: Root Documentation Files (2 files)

| #   | File                        | Size  | Last Modified | Reason for Deletion                                      |
| --- | --------------------------- | ----- | ------------- | -------------------------------------------------------- |
| 29  | `DEEPResearch.md`           | ~15KB | 2025-12-21    | Superseded by research_exchange_ecosystems_2025-01-17.md |
| 30  | `docs/performance_audit.md` | ~8KB  | 2025-12-21    | Old audit, superseded                                    |

### Files to Keep (15 files)

These are essential project documents that must be preserved:

| File                                                    | Purpose                                         | Priority |
| ------------------------------------------------------- | ----------------------------------------------- | -------- |
| `claudedocs/research_exchange_ecosystems_2025-01-17.md` | **SOURCE OF TRUTH** - Primary research document | CRITICAL |
| `docs/TECHNICAL.md`                                     | Technical architecture documentation            | HIGH     |
| `docs/USER_GUIDE.md`                                    | User guide and setup instructions               | HIGH     |
| `docs/SPEC_PANEL_REVIEW_2026-01-17.md`                  | Expert analysis and critical findings           | HIGH     |
| `docs/memory/README.md`                                 | Memory directory documentation                  | MEDIUM   |
| `docs/memory/EXCHANGE_API_REFERENCE.md`                 | Comprehensive API reference                     | HIGH     |
| `docs/memory/pm_context.md`                             | Project manager context                         | MEDIUM   |
| `docs/memory/SYMBOL_MAP.md`                             | Symbol mapping reference                        | MEDIUM   |
| `docs/memory/REPO_INDEX.md`                             | Codebase navigation index                       | HIGH     |
| `CLAUDE.md`                                             | Project instructions (SuperClaude)              | CRITICAL |
| `CONTRIBUTING.md`                                       | Contribution guidelines                         | HIGH     |
| `KNOWLEDGE.md`                                          | Technical knowledge base                        | HIGH     |
| `PLANNING.md`                                           | Architecture and design principles              | CRITICAL |
| `TASK.md`                                               | Current tasks and backlog                       | HIGH     |
| `README.md`                                             | Project overview                                | HIGH     |

### Files Requiring Review - Recommendations

| File                                    | Recommendation | Rationale                                                                                    |
| --------------------------------------- | -------------- | -------------------------------------------------------------------------------------------- |
| `docs/SPEC_PANEL_REVIEW_2026-01-17.md`  | **KEEP**       | Contains critical expert analysis of funding rate discrepancies and other important findings |
| `docs/memory/EXCHANGE_API_REFERENCE.md` | **KEEP**       | Comprehensive API documentation for both exchanges, actively used                            |
| `docs/memory/REPO_INDEX.md`             | **KEEP**       | Essential codebase navigation and reference guide                                            |
| `docs/memory/*.jsonl` files             | **KEEP**       | Auto-managed by Serena, contain learned patterns and metrics                                 |

---

## Risk Assessment

### Critical Dependencies Found

#### 1. Code Reference to Deleted File

**File**: `src/funding_bot/services/liquidity_gates_preflight.py:15`
**Reference**: `Ref: docs/memory/preflight_liquidity_implementation_plan.md`
**Impact**: Low - This is only a docstring comment, not a functional dependency
**Action Required**: Update the docstring to remove or update the reference

**Current Code**:

```python
"""
Liquidity Gates Preflight Module

Ref: docs/memory/preflight_liquidity_implementation_plan.md
"""
```

**Recommended Fix**:

```python
"""
Liquidity Gates Preflight Module

Implements pre-trade liquidity checks to ensure sufficient orderbook depth
before executing arbitrage opportunities.
"""
```

### Cross-References Among Deleted Files

Several claudedocs files reference each other. These will become broken links after deletion, but since all referenced files are also being deleted, this is not a concern.

**Example Cross-References**:

- `docs/memory/SESSION_SUMMARY_20260115.md` references:
  - `claudedocs/research_x10_liquidity_integration_20260115.md`
  - `claudedocs/x10_liquidity_fix_implementation_20260115.md`
  - `claudedocs/troubleshooting_ondo_orderbook_issue_20260115.md`
  - `claudedocs/ondo_orderbook_fix_complete_20260115.md`

Since all these files are being deleted together, no action is needed.

### Risk Summary

| Risk Category         | Level | Mitigation                                     |
| --------------------- | ----- | ---------------------------------------------- |
| **Code Dependencies** | LOW   | Only 1 docstring reference found, easily fixed |
| **Broken Links**      | LOW   | Only cross-references among deleted files      |
| **Lost Information**  | LOW   | All critical info preserved in kept files      |
| **Bot Functionality** | NONE  | No functional code dependencies                |
| **Test Impact**       | NONE  | No test files affected                         |

---

## Deletion Order Strategy

### Phase 1: Pre-Deletion Preparation (Safe)

1. **Create backup** of all files to be deleted
2. **Update code reference** in `liquidity_gates_preflight.py`
3. **Verify backup integrity**

### Phase 2: Delete Session Files First (Low Risk)

Delete session and temporary files first as they have the fewest dependencies:

**Order**:

1. `docs/memory/SESSION_SUMMARY_20260115.md`
2. `docs/memory/SESSION_2026-01-15_PHASE2_REFACTORING.md`
3. `docs/memory/preflight_liquidity_implementation_plan.md`
4. `claudedocs/git_commit_summary_20260113.md`
5. `claudedocs/git_commit_summary_20260114.md`

### Phase 3: Delete Implementation/Analysis Files (Medium Risk)

Delete completed implementation and analysis files:

**Order**: 6. `claudedocs/analysis_funding_cadence_blocker_20260113.md` 7. `claudedocs/analyze_offline_first_import_guard_fix_20260114.md` 8. `claudedocs/fix_offline_tests_sdk_import_guard_20260113.md` 9. `claudedocs/implement_import_guard_verification_20260114.md` 10. `claudedocs/offline_first_import_guard_verification_20260114.md` 11. `claudedocs/quality_gate_reflection_20260113.md` 12. `claudedocs/quality_gate_reflection_20260114.md`

### Phase 4: Delete Research and Phase Files (Medium Risk)

Delete research documents and phase completion files:

**Order**: 13. `claudedocs/research_funding_cadence_verification_20260113.md` 14. `claudedocs/research_orderbook_depth_api_20260115.md` 15. `claudedocs/research_x10_liquidity_integration_20260115.md` 16. `claudedocs/EXCHANGE_RATE_LIMITS_RESEARCH_20260114.md` 17. `claudedocs/PHASE_1_PERFORMANCE_ANALYSIS_20260114.md` 18. `claudedocs/PHASE_1_PREFLIGHT_LIQUIDITY_COMPLETE_20260115.md` 19. `claudedocs/PHASE_1_PREMIUM_OPTIMIZED_PLAN_20260114.md` 20. `claudedocs/PHASE_COMPLETE_PREFLIGHT_LIQUIDITY_20260115.md`

### Phase 5: Delete Verification and Test Reports (Low Risk)

Delete old verification and test reports:

**Order**: 21. `claudedocs/BACKILL_VERIFICATION_REPORT_20250115.md` 22. `claudedocs/bot_behavior_verification_table_20260114.md` 23. `claudedocs/test_report_offline_20260113.md` 24. `claudedocs/test_report_offline_20260114.md`

### Phase 6: Delete Fix and Troubleshooting Files (Low Risk)

Delete completed fix documentation:

**Order**: 25. `claudedocs/ondo_orderbook_fix_complete_20260115.md` 26. `claudedocs/troubleshooting_ondo_orderbook_issue_20260115.md` 27. `claudedocs/x10_liquidity_fix_implementation_20260115.md`

### Phase 7: Delete Root Documentation Files (Low Risk)

Delete superseded root-level documentation:

**Order**: 28. `claudedocs/PLAN_ORDERBOOK_OPTIMIZATION_20260115.md` 29. `DEEPResearch.md` 30. `docs/performance_audit.md`

### Phase 8: Post-Deletion Verification

1. **Run all unit tests** to ensure no breakage
2. **Verify bot startup** works correctly
3. **Check for broken links** in remaining documentation
4. **Commit changes** with clear message

---

## Backup Strategy

### Pre-Deletion Backup

**Location**: Create a dedicated backup directory outside the project

```bash
# Create backup directory
mkdir -p ~/backups/funding-bot-docs-cleanup-2026-01-17

# Copy all files to be deleted
mkdir -p ~/backups/funding-bot-docs-cleanup-2026-01-17/claudedocs
mkdir -p ~/backups/funding-bot-docs-cleanup-2026-01-17/docs/memory

# Copy claudedocs files (25 files)
cp claudedocs/analysis_funding_cadence_blocker_20260113.md \
   claudedocs/analyze_offline_first_import_guard_fix_20260114.md \
   claudedocs/BACKILL_VERIFICATION_REPORT_20250115.md \
   claudedocs/bot_behavior_verification_table_20260114.md \
   claudedocs/EXCHANGE_RATE_LIMITS_RESEARCH_20260114.md \
   claudedocs/fix_offline_tests_sdk_import_guard_20260113.md \
   claudedocs/git_commit_summary_20260113.md \
   claudedocs/git_commit_summary_20260114.md \
   claudedocs/implement_import_guard_verification_20260114.md \
   claudedocs/offline_first_import_guard_verification_20260114.md \
   claudedocs/ondo_orderbook_fix_complete_20260115.md \
   claudedocs/PHASE_1_PERFORMANCE_ANALYSIS_20260114.md \
   claudedocs/PHASE_1_PREFLIGHT_LIQUIDITY_COMPLETE_20260115.md \
   claudedocs/PHASE_1_PREMIUM_OPTIMIZED_PLAN_20260114.md \
   claudedocs/PHASE_COMPLETE_PREFLIGHT_LIQUIDITY_20260115.md \
   claudedocs/PLAN_ORDERBOOK_OPTIMIZATION_20260115.md \
   claudedocs/quality_gate_reflection_20260113.md \
   claudedocs/quality_gate_reflection_20260114.md \
   claudedocs/research_funding_cadence_verification_20260113.md \
   claudedocs/research_orderbook_depth_api_20260115.md \
   claudedocs/research_x10_liquidity_integration_20260115.md \
   claudedocs/test_report_offline_20260113.md \
   claudedocs/test_report_offline_20260114.md \
   claudedocs/troubleshooting_ondo_orderbook_issue_20260115.md \
   claudedocs/x10_liquidity_fix_implementation_20260115.md \
   ~/backups/funding-bot-docs-cleanup-2026-01-17/claudedocs/

# Copy docs/memory files (3 files)
cp docs/memory/preflight_liquidity_implementation_plan.md \
   docs/memory/SESSION_SUMMARY_20260115.md \
   docs/memory/SESSION_2026-01-15_PHASE2_REFACTORING.md \
   ~/backups/funding-bot-docs-cleanup-2026-01-17/docs/memory/

# Copy root files (2 files)
cp DEEPResearch.md docs/performance_audit.md \
   ~/backups/funding-bot-docs-cleanup-2026-01-17/

# Create backup manifest
cat > ~/backups/funding-bot-docs-cleanup-2026-01-17/BACKUP_MANIFEST.txt << 'EOF'
Backup Created: 2026-01-17
Files Backed Up: 30
Purpose: Documentation cleanup - safe deletion of outdated files

Files List:
- 25 claudedocs/*.md files
- 3 docs/memory/*.md files
- 2 root-level files

Restore Instructions:
1. Copy files back to original locations
2. Verify file integrity
3. Update git if needed
EOF

# Verify backup
ls -lh ~/backups/funding-bot-docs-cleanup-2026-01-17/
find ~/backups/funding-bot-docs-cleanup-2026-01-17/ -type f | wc -l
```

### Backup Retention Policy

- **Keep backup for**: 90 days minimum
- **Location**: External to project directory
- **Verification**: Check file count (should be 30 files)
- **Compression**: Optional, but recommended for long-term storage

### Git Backup Alternative

If using Git, create a dedicated branch:

```bash
# Create backup branch
git checkout -b backup/docs-cleanup-2026-01-17

# Commit current state before deletion
git add .
git commit -m "Backup: Documentation state before cleanup - 2026-01-17"

# Push to remote for safety
git push origin backup/docs-cleanup-2026-01-17

# Switch back to main branch
git checkout main
```

---

## Verification Strategy

### Pre-Deletion Verification

1. **Verify backup exists and contains all 30 files**

   ```bash
   find ~/backups/funding-bot-docs-cleanup-2026-01-17/ -type f | wc -l
   # Should output: 30
   ```

2. **Run baseline tests** to ensure bot works before deletion

   ```bash
   pytest tests/unit/ -q
   # Should pass with current code
   ```

3. **Document current state**
   - Note any warnings or issues
   - Record test results
   - Snapshot of file structure

### Post-Deletion Verification

#### Step 1: Code Integrity Check

```bash
# Check for any Python import errors
python -m py_compile src/funding_bot/**/*.py

# Verify the docstring update was successful
grep -n "preflight_liquidity_implementation_plan" src/funding_bot/services/liquidity_gates_preflight.py
# Should return nothing (reference removed)
```

#### Step 2: Test Suite Verification

```bash
# Run all unit tests (must pass)
pytest tests/unit/ -v

# Run integration tests if available
pytest tests/integration/ -v

# Check for any test failures related to documentation
pytest tests/ -k doc -v
```

#### Step 3: Bot Startup Verification

```bash
# Test bot initialization (dry-run mode)
python -m funding_bot --help

# Verify configuration loads correctly
python -c "from funding_bot.config.settings import Settings; print('Config OK')"

# Check that all critical documentation is accessible
ls -l claudedocs/research_exchange_ecosystems_2025-01-17.md
ls -l docs/TECHNICAL.md
ls -l docs/USER_GUIDE.md
ls -l docs/SPEC_PANEL_REVIEW_2026-01-17.md
```

#### Step 4: Documentation Link Check

```bash
# Check for broken links in remaining markdown files
# (Optional: Use markdown-link-check tool if available)

# Verify all kept files exist
for file in claudedocs/research_exchange_ecosystems_2025-01-17.md \
            docs/TECHNICAL.md \
            docs/USER_GUIDE.md \
            docs/SPEC_PANEL_REVIEW_2026-01-17.md \
            docs/memory/EXCHANGE_API_REFERENCE.md \
            docs/memory/REPO_INDEX.md \
            CLAUDE.md CONTRIBUTING.md KNOWLEDGE.md \
            PLANNING.md TASK.md README.md; do
    if [ ! -f "$file" ]; then
        echo "ERROR: Missing critical file: $file"
        exit 1
    fi
done
echo "All critical files present"
```

#### Step 5: Git Status Check

```bash
# Verify only expected files were deleted
git status

# Should show:
# - 30 files deleted
# - 1 file modified (liquidity_gates_preflight.py)
# - No unexpected changes
```

### Rollback Procedure

If any verification step fails:

```bash
# Stop immediately
echo "Verification failed - initiating rollback"

# Restore from backup
cp -r ~/backups/funding-bot-docs-cleanup-2026-01-17/claudedocs/* claudedocs/
cp -r ~/backups/funding-bot-docs-cleanup-2026-01-17/docs/memory/* docs/memory/
cp ~/backups/funding-bot-docs-cleanup-2026-01-17/DEEPResearch.md .
cp ~/backups/funding-bot-docs-cleanup-2026-01-17/performance_audit.md docs/

# Revert code changes
git checkout src/funding_bot/services/liquidity_gates_preflight.py

# Verify restoration
git status
pytest tests/unit/ -q

# Document rollback
echo "Rollback completed at $(date)" >> ~/backups/funding-bot-docs-cleanup-2026-01-17/ROLLBACK_LOG.txt
```

---

## Implementation Checklist

### Pre-Execution Checklist

- [ ] Backup directory created outside project
- [ ] All 30 files copied to backup
- [ ] Backup verified (file count = 30)
- [ ] Baseline tests run and passing
- [ ] Current state documented
- [ ] Git backup branch created (optional)

### Execution Checklist

- [ ] Docstring reference updated in `liquidity_gates_preflight.py`
- [ ] Phase 1 files deleted (5 files)
- [ ] Phase 2 files deleted (6 files)
- [ ] Phase 3 files deleted (8 files)
- [ ] Phase 4 files deleted (4 files)
- [ ] Phase 5 files deleted (3 files)
- [ ] Phase 6 files deleted (3 files)
- [ ] Phase 7 files deleted (1 file)
- [ ] Total deleted: 30 files

### Post-Execution Checklist

- [ ] Python syntax check passes
- [ ] All unit tests pass
- [ ] Bot startup works correctly
- [ ] All critical documentation files present
- [ ] No broken links in remaining docs
- [ ] Git status shows expected changes
- [ ] Changes committed with clear message
- [ ] Backup retention date noted (90 days)

---

## Commit Message Template

```bash
git add .
git commit -m "docs(cleanup): Remove 30 outdated documentation files

This commit removes outdated analysis, implementation notes, and session
summaries that have been superseded by the source of truth document
(claudedocs/research_exchange_ecosystems_2025-01-17.md).

Files Removed (30):
- 25 claudedocs/*.md analysis and implementation files
- 3 docs/memory/*.md session and temporary files
- 2 root-level superseded files (DEEPResearch.md, docs/performance_audit.md)

Code Changes:
- Updated docstring in liquidity_gates_preflight.py to remove
  reference to deleted docs/memory/preflight_liquidity_implementation_plan.md

Files Preserved:
- All 15 essential project documents maintained
- 4 files from review list kept (SPEC_PANEL_REVIEW, EXCHANGE_API_REFERENCE,
  REPO_INDEX, and auto-managed .jsonl files)

Backup:
- Full backup created at ~/backups/funding-bot-docs-cleanup-2026-01-17/
- Retention period: 90 days

Verification:
- All unit tests passing
- Bot startup verified
- No functional code dependencies affected

Related: N/A
"
```

---

## Post-Cleanup Documentation Structure

### Final Documentation Hierarchy

```
funding-bot/
├── claudedocs/
│   └── research_exchange_ecosystems_2025-01-17.md  [SOURCE OF TRUTH]
├── docs/
│   ├── TECHNICAL.md
│   ├── USER_GUIDE.md
│   ├── SPEC_PANEL_REVIEW_2026-01-17.md
│   └── memory/
│       ├── README.md
│       ├── EXCHANGE_API_REFERENCE.md
│       ├── pm_context.md
│       ├── SYMBOL_MAP.md
│       ├── REPO_INDEX.md
│       ├── patterns_learned.jsonl          [Serena-managed]
│       ├── solutions_learned.jsonl         [Serena-managed]
│       ├── workflow_metrics.jsonl          [Serena-managed]
│       └── reflexion.jsonl.example        [Example file]
├── CLAUDE.md
├── CONTRIBUTING.md
├── KNOWLEDGE.md
├── PLANNING.md
├── TASK.md
└── README.md
```

### Documentation Coverage

| Category                  | Files    | Status               |
| ------------------------- | -------- | -------------------- |
| **Core Documentation**    | 5 files  | ✅ Complete          |
| **Technical Reference**   | 3 files  | ✅ Complete          |
| **Memory & Context**      | 6 files  | ✅ Complete          |
| **SuperClaude Framework** | 4 files  | ✅ Complete          |
| **Total**                 | 18 files | ✅ Clean & Organized |

---

## Timeline Estimation

| Phase             | Estimated Time | Notes                           |
| ----------------- | -------------- | ------------------------------- |
| **Preparation**   | 15 minutes     | Backup creation, baseline tests |
| **Code Update**   | 5 minutes      | Update docstring reference      |
| **File Deletion** | 10 minutes     | Delete 30 files in phases       |
| **Verification**  | 20 minutes     | Tests, startup, link checks     |
| **Commit**        | 5 minutes      | Stage and commit changes        |
| **Total**         | ~55 minutes    | Approximately 1 hour            |

---

## Success Criteria

The cleanup will be considered successful when:

1. ✅ All 30 files deleted without errors
2. ✅ Backup verified and stored safely
3. ✅ All unit tests pass (394 tests)
4. ✅ Bot starts up correctly in dry-run mode
5. ✅ All 15 critical documentation files present
6. ✅ No broken links in remaining documentation
7. ✅ Git commit created with clear message
8. ✅ No functional code dependencies broken

---

## Frequently Asked Questions

### Q: What if I need information from a deleted file later?

**A**: All files are backed up to `~/backups/funding-bot-docs-cleanup-2026-01-17/` and will be retained for 90 days. You can restore individual files or the entire backup if needed.

### Q: Will this affect the bot's functionality?

**A**: No. The only code change is a docstring update in `liquidity_gates_preflight.py`, which is a comment and has no functional impact. All functional code remains unchanged.

### Q: What about the .jsonl files in docs/memory?

**A**: These are auto-managed by Serena and should NOT be deleted. They contain learned patterns, solutions, and workflow metrics that are actively used.

### Q: Can I delete files in a different order?

**A**: Yes, the order is a recommendation for safety. The most important thing is to create a backup first and verify after each phase. The suggested order minimizes risk by starting with files that have the fewest dependencies.

### Q: What if tests fail after deletion?

**A**: If tests fail, immediately rollback using the procedure in the "Rollback Procedure" section. Investigate the failure, fix any issues, and retry the cleanup.

### Q: Should I commit the backup directory?

**A**: No. The backup directory should be kept outside the project repository. If you want a Git backup, use the dedicated branch approach described in the "Git Backup Alternative" section.

---

## Appendix A: File Deletion Commands

### Complete Deletion Script

```bash
#!/bin/bash
# Documentation Cleanup Script
# Usage: ./cleanup_docs.sh

set -e  # Exit on error

BACKUP_DIR="$HOME/backups/funding-bot-docs-cleanup-2026-01-17"

echo "=== Documentation Cleanup Script ==="
echo "Date: $(date)"
echo ""

# Phase 0: Verify backup exists
if [ ! -d "$BACKUP_DIR" ]; then
    echo "ERROR: Backup directory not found: $BACKUP_DIR"
    echo "Please create backup before running this script."
    exit 1
fi

FILE_COUNT=$(find "$BACKUP_DIR" -type f | wc -l)
if [ "$FILE_COUNT" -ne 30 ]; then
    echo "ERROR: Backup contains $FILE_COUNT files, expected 30"
    exit 1
fi

echo "✓ Backup verified ($FILE_COUNT files)"
echo ""

# Phase 1: Update code reference
echo "Phase 1: Updating code reference..."
# (Manual step - update docstring in liquidity_gates_preflight.py)
echo "  Please manually update the docstring in:"
echo "  src/funding_bot/services/liquidity_gates_preflight.py"
echo ""
read -p "Press Enter after updating docstring..."

# Phase 2: Delete session files
echo "Phase 2: Deleting session files..."
rm -f docs/memory/SESSION_SUMMARY_20260115.md
rm -f docs/memory/SESSION_2026-01-15_PHASE2_REFACTORING.md
rm -f docs/memory/preflight_liquidity_implementation_plan.md
rm -f claudedocs/git_commit_summary_20260113.md
rm -f claudedocs/git_commit_summary_20260114.md
echo "  ✓ Deleted 5 session files"

# Phase 3: Delete implementation/analysis files
echo "Phase 3: Deleting implementation/analysis files..."
rm -f claudedocs/analysis_funding_cadence_blocker_20260113.md
rm -f claudedocs/analyze_offline_first_import_guard_fix_20260114.md
rm -f claudedocs/fix_offline_tests_sdk_import_guard_20260113.md
rm -f claudedocs/implement_import_guard_verification_20260114.md
rm -f claudedocs/offline_first_import_guard_verification_20260114.md
rm -f claudedocs/quality_gate_reflection_20260113.md
rm -f claudedocs/quality_gate_reflection_20260114.md
echo "  ✓ Deleted 6 implementation/analysis files"

# Phase 4: Delete research and phase files
echo "Phase 4: Deleting research and phase files..."
rm -f claudedocs/research_funding_cadence_verification_20260113.md
rm -f claudedocs/research_orderbook_depth_api_20260115.md
rm -f claudedocs/research_x10_liquidity_integration_20260115.md
rm -f claudedocs/EXCHANGE_RATE_LIMITS_RESEARCH_20260114.md
rm -f claudedocs/PHASE_1_PERFORMANCE_ANALYSIS_20260114.md
rm -f claudedocs/PHASE_1_PREFLIGHT_LIQUIDITY_COMPLETE_20260115.md
rm -f claudedocs/PHASE_1_PREMIUM_OPTIMIZED_PLAN_20260114.md
rm -f claudedocs/PHASE_COMPLETE_PREFLIGHT_LIQUIDITY_20260115.md
echo "  ✓ Deleted 8 research/phase files"

# Phase 5: Delete verification and test reports
echo "Phase 5: Deleting verification and test reports..."
rm -f claudedocs/BACKILL_VERIFICATION_REPORT_20250115.md
rm -f claudedocs/bot_behavior_verification_table_20260114.md
rm -f claudedocs/test_report_offline_20260113.md
rm -f claudedocs/test_report_offline_20260114.md
echo "  ✓ Deleted 4 verification/test report files"

# Phase 6: Delete fix and troubleshooting files
echo "Phase 6: Deleting fix and troubleshooting files..."
rm -f claudedocs/ondo_orderbook_fix_complete_20260115.md
rm -f claudedocs/troubleshooting_ondo_orderbook_issue_20260115.md
rm -f claudedocs/x10_liquidity_fix_implementation_20260115.md
echo "  ✓ Deleted 3 fix/troubleshooting files"

# Phase 7: Delete root documentation files
echo "Phase 7: Deleting root documentation files..."
rm -f claudedocs/PLAN_ORDERBOOK_OPTIMIZATION_20260115.md
rm -f DEEPResearch.md
rm -f docs/performance_audit.md
echo "  ✓ Deleted 3 root documentation files"

# Summary
echo ""
echo "=== Cleanup Complete ==="
echo "Total files deleted: 30"
echo ""
echo "Next steps:"
echo "1. Run: pytest tests/unit/ -q"
echo "2. Run: python -m funding_bot --help"
echo "3. Run: git status"
echo "4. Commit changes with provided message"
echo ""
```

---

## Appendix B: Verification Script

```bash
#!/bin/bash
# Post-Cleanup Verification Script
# Usage: ./verify_cleanup.sh

set -e

echo "=== Post-Cleanup Verification ==="
echo "Date: $(date)"
echo ""

# Check 1: Verify deleted files are gone
echo "Check 1: Verifying deleted files are removed..."
DELETED_FILES=(
    "claudedocs/analysis_funding_cadence_blocker_20260113.md"
    "claudedocs/analyze_offline_first_import_guard_fix_20260114.md"
    "claudedocs/BACKILL_VERIFICATION_REPORT_20250115.md"
    # ... (add all 30 files)
)

for file in "${DELETED_FILES[@]}"; do
    if [ -f "$file" ]; then
        echo "  ✗ ERROR: File still exists: $file"
        exit 1
    fi
done
echo "  ✓ All 30 files successfully deleted"
echo ""

# Check 2: Verify critical files exist
echo "Check 2: Verifying critical files are present..."
CRITICAL_FILES=(
    "claudedocs/research_exchange_ecosystems_2025-01-17.md"
    "docs/TECHNICAL.md"
    "docs/USER_GUIDE.md"
    "docs/SPEC_PANEL_REVIEW_2026-01-17.md"
    "docs/memory/EXCHANGE_API_REFERENCE.md"
    "docs/memory/REPO_INDEX.md"
    "CLAUDE.md"
    "CONTRIBUTING.md"
    "KNOWLEDGE.md"
    "PLANNING.md"
    "TASK.md"
    "README.md"
)

for file in "${CRITICAL_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo "  ✗ ERROR: Critical file missing: $file"
        exit 1
    fi
done
echo "  ✓ All 12 critical files present"
echo ""

# Check 3: Verify code reference updated
echo "Check 3: Verifying code reference updated..."
if grep -q "preflight_liquidity_implementation_plan" src/funding_bot/services/liquidity_gates_preflight.py; then
    echo "  ✗ ERROR: Docstring reference not removed"
    exit 1
fi
echo "  ✓ Docstring reference removed"
echo ""

# Check 4: Run unit tests
echo "Check 4: Running unit tests..."
if ! pytest tests/unit/ -q; then
    echo "  ✗ ERROR: Unit tests failed"
    exit 1
fi
echo "  ✓ All unit tests passing"
echo ""

# Check 5: Verify bot startup
echo "Check 5: Verifying bot startup..."
if ! python -m funding_bot --help > /dev/null 2>&1; then
    echo "  ✗ ERROR: Bot startup failed"
    exit 1
fi
echo "  ✓ Bot startup successful"
echo ""

echo "=== All Verification Checks Passed ==="
echo ""
echo "Cleanup verified successfully!"
echo "You may now commit the changes."
```

---

## Conclusion

This cleanup plan provides a comprehensive, safe approach to removing 30 outdated documentation files while preserving all essential project information. The risk is minimal, with only one docstring reference requiring update. Following this plan will result in a cleaner, more maintainable documentation structure without affecting bot functionality.

**Next Steps**:

1. Review this plan thoroughly
2. Create the backup as specified
3. Execute the deletion in phases
4. Run verification scripts
5. Commit changes with the provided message

**Contact**: If any issues arise during cleanup, refer to the rollback procedure or consult the backup.

---

_Document Version: 1.0_
_Last Updated: 2026-01-17_
_Status: Ready for Execution_
