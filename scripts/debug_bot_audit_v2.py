#!/usr/bin/env python3
"""
Debug Bot Audit Script v2
========================

Comprehensive audit tool for the Funding Arbitrage Bot.
Scans Python code, validates configs, parses logs, and compares with TS SDKs.

Usage:
    python scripts/debug_bot_audit_v2.py \
        --log logs/funding_bot_LEON_20251214_110505_FULL.log \
        --ts-lighter C:\\Users\\koopm\\Desktop\\lighter-ts-main \
        --ts-x10 C:\\Users\\koopm\\Desktop\\Extended-TS-SDK-master \
        --output audit_report.md

Author: Funding Bot Audit System
Version: 2.0.0
"""

import argparse
import ast
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any


# =============================================================================
# CONSTANTS
# =============================================================================

CRITICAL_CALLS = [
    "create_order", "cancel_order", "cancel_all_orders",
    "open_live_position", "close_live_position",
    "get_next_nonce", "_get_next_nonce", "acknowledge_failure",
    "subscribe", "send", "fetch_open_positions",
]

SDK_LIMITS = {
    "lighter": {
        "nonce_batch_size": 20,
        "nonce_refill_threshold": 2,
        "nonce_max_cache_age": 30.0,
        "rate_limit_per_second": 2.5,
        "ws_stale_threshold": 180.0,
    },
    "x10": {
        "ping_interval": 15.0,
        "stale_threshold": 300.0,
        "min_notional": 10.0,
    }
}

LOG_PATTERNS = {
    "error": re.compile(r"\[ERROR\]", re.IGNORECASE),
    "warning": re.compile(r"\[WARNING\]", re.IGNORECASE),
    "rate_limit": re.compile(r"429|rate.?limit", re.IGNORECASE),
    "ws_disconnect": re.compile(r"1006|Connection closed|WebSocket closed"),
    "shutdown_start": re.compile(r"Shutdown orchestrator|STOPPING BOT|Graceful Shutdown"),
    "shutdown_end": re.compile(r"All positions closed|Shutdown complete"),
    "trade_success": re.compile(r"TRADE SUMMARY.*SUCCESS|Hedged trade complete"),
    "trade_fail": re.compile(r"TRADE SUMMARY.*FAIL|TIMEOUT|ROLLBACK"),
    "nonce_refill": re.compile(r"Nonce pool refilled.*(\d+) nonces"),
    "fill_detected": re.compile(r"Fill detected after (\d+) checks.*\(([\d.]+)s"),
    "ghost_fill": re.compile(r"GHOST FILL"),
    "startup_ready": re.compile(r"BOT V5 RUNNING|Started \d+ tasks"),
}


# =============================================================================
# LOG PARSER
# =============================================================================

class LogParser:
    """Parse and analyze bot log files."""
    
    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self.lines: List[str] = []
        self.metrics: Dict[str, Any] = {}
        
    def parse(self) -> Dict[str, Any]:
        """Parse the log file and extract metrics."""
        if not self.log_path.exists():
            return {"error": f"Log file not found: {self.log_path}"}
        
        with open(self.log_path, "r", encoding="utf-8", errors="ignore") as f:
            self.lines = f.readlines()
        
        self.metrics = {
            "total_lines": len(self.lines),
            "errors": [],
            "warnings": [],
            "rate_limits": [],
            "ws_disconnects": [],
            "trades": {"success": 0, "fail": 0, "trades": []},
            "nonce_batches": [],
            "fill_times": [],
            "ghost_fills": 0,
            "startup_time": None,
            "shutdown_time": None,
            "session_duration": None,
        }
        
        startup_line = None
        shutdown_start = None
        shutdown_end = None
        first_timestamp = None
        last_timestamp = None
        
        for i, line in enumerate(self.lines):
            line_num = i + 1
            
            # Extract timestamp
            ts_match = re.match(r"(\d{2}:\d{2}:\d{2})", line)
            if ts_match:
                ts = ts_match.group(1)
                if first_timestamp is None:
                    first_timestamp = ts
                last_timestamp = ts
            
            # Check patterns
            if LOG_PATTERNS["error"].search(line):
                self.metrics["errors"].append({"line": line_num, "content": line.strip()[:200]})
            
            if LOG_PATTERNS["warning"].search(line):
                self.metrics["warnings"].append({"line": line_num, "content": line.strip()[:200]})
            
            if LOG_PATTERNS["rate_limit"].search(line):
                self.metrics["rate_limits"].append({"line": line_num, "content": line.strip()[:100]})
            
            if LOG_PATTERNS["ws_disconnect"].search(line):
                self.metrics["ws_disconnects"].append({"line": line_num, "content": line.strip()[:100]})
            
            if LOG_PATTERNS["trade_success"].search(line):
                self.metrics["trades"]["success"] += 1
                self.metrics["trades"]["trades"].append({"line": line_num, "result": "SUCCESS"})
            
            if LOG_PATTERNS["trade_fail"].search(line):
                self.metrics["trades"]["fail"] += 1
                self.metrics["trades"]["trades"].append({"line": line_num, "result": "FAIL"})
            
            if match := LOG_PATTERNS["nonce_refill"].search(line):
                self.metrics["nonce_batches"].append(int(match.group(1)))
            
            if match := LOG_PATTERNS["fill_detected"].search(line):
                self.metrics["fill_times"].append({
                    "checks": int(match.group(1)),
                    "time_s": float(match.group(2)),
                })
            
            if LOG_PATTERNS["ghost_fill"].search(line):
                self.metrics["ghost_fills"] += 1
            
            if LOG_PATTERNS["startup_ready"].search(line) and startup_line is None:
                startup_line = line_num
                self.metrics["startup_time"] = ts if ts_match else None
            
            if LOG_PATTERNS["shutdown_start"].search(line) and shutdown_start is None:
                shutdown_start = ts if ts_match else None
            
            if LOG_PATTERNS["shutdown_end"].search(line):
                shutdown_end = ts if ts_match else None
                # Extract elapsed time if present
                elapsed_match = re.search(r"elapsed=([\d.]+)s", line)
                if elapsed_match:
                    self.metrics["shutdown_elapsed"] = float(elapsed_match.group(1))
        
        # Calculate session duration
        if first_timestamp and last_timestamp:
            try:
                t1 = datetime.strptime(first_timestamp, "%H:%M:%S")
                t2 = datetime.strptime(last_timestamp, "%H:%M:%S")
                self.metrics["session_duration"] = (t2 - t1).total_seconds()
            except ValueError:
                pass
        
        return self.metrics


# =============================================================================
# PYTHON CODE ANALYZER
# =============================================================================

class PythonCodeAnalyzer:
    """Analyze Python source files using AST."""
    
    def __init__(self, src_dir: str):
        self.src_dir = Path(src_dir)
        self.findings: Dict[str, List[Dict]] = defaultdict(list)
        
    def analyze(self) -> Dict[str, List[Dict]]:
        """Scan all Python files for critical calls."""
        for py_file in self.src_dir.rglob("*.py"):
            self._analyze_file(py_file)
        return dict(self.findings)
    
    def _analyze_file(self, file_path: Path) -> None:
        """Analyze a single Python file."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
            
            tree = ast.parse(source, filename=str(file_path))
            
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func_name = self._get_func_name(node)
                    if func_name and any(c in func_name for c in CRITICAL_CALLS):
                        self.findings[func_name].append({
                            "file": str(file_path.relative_to(self.src_dir.parent)),
                            "line": node.lineno,
                            "call": func_name,
                        })
                        
        except (SyntaxError, UnicodeDecodeError) as e:
            self.findings["_errors"].append({
                "file": str(file_path),
                "error": str(e),
            })
    
    def _get_func_name(self, node: ast.Call) -> Optional[str]:
        """Extract function name from Call node."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None


# =============================================================================
# TS SDK COMPARATOR
# =============================================================================

class TsSdkComparator:
    """Compare Python implementation with TypeScript SDKs."""
    
    def __init__(self, ts_lighter_path: Optional[str], ts_x10_path: Optional[str]):
        self.ts_lighter = Path(ts_lighter_path) if ts_lighter_path else None
        self.ts_x10 = Path(ts_x10_path) if ts_x10_path else None
        self.comparison: Dict[str, Any] = {}
        
    def compare(self) -> Dict[str, Any]:
        """Compare SDKs and return findings."""
        if self.ts_lighter:
            self.comparison["lighter"] = self._analyze_ts_sdk(self.ts_lighter, "lighter")
        if self.ts_x10:
            self.comparison["x10"] = self._analyze_ts_sdk(self.ts_x10, "x10")
        return self.comparison
    
    def _analyze_ts_sdk(self, sdk_path: Path, sdk_name: str) -> Dict[str, Any]:
        """Analyze a TypeScript SDK directory."""
        findings = {
            "exists": sdk_path.exists(),
            "modules": [],
            "patterns": [],
        }
        
        if not sdk_path.exists():
            return findings
        
        # Find key modules
        key_patterns = {
            "lighter": ["nonce-manager.ts", "nonce-cache.ts", "request-batcher.ts", "ws-client.ts"],
            "x10": ["stream-client.ts", "trading-client", "nonce.ts", "withdrawals.ts"],
        }
        
        for pattern in key_patterns.get(sdk_name, []):
            matches = list(sdk_path.rglob(pattern))
            if matches:
                findings["modules"].append({
                    "name": pattern,
                    "path": str(matches[0].relative_to(sdk_path)),
                    "size": matches[0].stat().st_size if matches[0].is_file() else "dir",
                })
        
        # Extract key patterns from nonce-manager if present
        if sdk_name == "lighter":
            nonce_file = sdk_path / "src" / "utils" / "nonce-manager.ts"
            if nonce_file.exists():
                content = nonce_file.read_text(encoding="utf-8")
                if "batchSize: 20" in content or "batchSize = 20" in content:
                    findings["patterns"].append("NONCE_BATCH_SIZE=20")
                if "maxCacheAge: 30000" in content or "30000" in content:
                    findings["patterns"].append("NONCE_MAX_CACHE_AGE=30s")
        
        return findings


# =============================================================================
# CONFIG VALIDATOR
# =============================================================================

class ConfigValidator:
    """Validate bot config against SDK limits."""
    
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.issues: List[Dict] = []
        
    def validate(self) -> List[Dict]:
        """Validate config and return issues."""
        if not self.config_path.exists():
            return [{"error": f"Config not found: {self.config_path}"}]
        
        try:
            content = self.config_path.read_text(encoding="utf-8")
            
            # Check for common issues
            checks = [
                (r"LIVE_TRADING\s*=\s*True", "LIVE_TRADING is enabled", "warning"),
                (r"RATE_LIMIT.*=.*0", "Rate limit may be disabled", "error"),
                (r"DEBUG\s*=\s*True", "Debug mode enabled", "info"),
            ]
            
            for pattern, message, level in checks:
                if re.search(pattern, content, re.IGNORECASE):
                    self.issues.append({"level": level, "message": message})
                    
        except Exception as e:
            self.issues.append({"error": str(e)})
        
        return self.issues


# =============================================================================
# REPORT GENERATOR
# =============================================================================

class ReportGenerator:
    """Generate Markdown audit report."""
    
    def __init__(self):
        self.sections: List[str] = []
        
    def add_section(self, title: str, content: str) -> None:
        """Add a section to the report."""
        self.sections.append(f"## {title}\n\n{content}\n")
        
    def add_table(self, title: str, headers: List[str], rows: List[List[str]]) -> None:
        """Add a table section."""
        header_row = "| " + " | ".join(headers) + " |"
        separator = "| " + " | ".join(["---"] * len(headers)) + " |"
        data_rows = "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in rows)
        
        content = f"{header_row}\n{separator}\n{data_rows}"
        self.add_section(title, content)
        
    def generate(self, output_path: Optional[str] = None) -> str:
        """Generate the full report."""
        report = f"""# 🔍 Funding Bot Audit Report

Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

---

{"".join(self.sections)}

---

*Generated by debug_bot_audit_v2.py*
"""
        
        if output_path:
            Path(output_path).write_text(report, encoding="utf-8")
            print(f"✅ Report saved to: {output_path}")
        
        return report


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive Funding Bot Audit Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--log", "-l", required=True, help="Path to bot log file")
    parser.add_argument("--ts-lighter", help="Path to lighter-ts-main SDK")
    parser.add_argument("--ts-x10", help="Path to Extended-TS-SDK-master")
    parser.add_argument("--src", default="src", help="Path to Python source directory")
    parser.add_argument("--config", default="config.py", help="Path to config file")
    parser.add_argument("--output", "-o", help="Output report path (optional)")
    
    args = parser.parse_args()
    
    print("🔍 Starting Funding Bot Audit...")
    print("=" * 60)
    
    report = ReportGenerator()
    
    # 1. Log Analysis
    print("\n📊 Parsing log file...")
    log_parser = LogParser(args.log)
    log_metrics = log_parser.parse()
    
    report.add_table("Log Analysis Summary", 
        ["Metric", "Value", "Status"],
        [
            ["Total Lines", str(log_metrics.get("total_lines", 0)), "ℹ️"],
            ["Errors", str(len(log_metrics.get("errors", []))), "✅" if len(log_metrics.get("errors", [])) == 0 else "❌"],
            ["Warnings", str(len(log_metrics.get("warnings", []))), "✅" if len(log_metrics.get("warnings", [])) == 0 else "⚠️"],
            ["Rate Limits", str(len(log_metrics.get("rate_limits", []))), "✅" if len(log_metrics.get("rate_limits", [])) == 0 else "❌"],
            ["WS Disconnects", str(len(log_metrics.get("ws_disconnects", []))), "ℹ️"],
            ["Trades Success", str(log_metrics.get("trades", {}).get("success", 0)), "✅"],
            ["Trades Fail", str(log_metrics.get("trades", {}).get("fail", 0)), "⚠️" if log_metrics.get("trades", {}).get("fail", 0) > 0 else "✅"],
            ["Ghost Fills", str(log_metrics.get("ghost_fills", 0)), "⚠️" if log_metrics.get("ghost_fills", 0) > 0 else "✅"],
            ["Shutdown Time", f"{log_metrics.get('shutdown_elapsed', 'N/A')}s", "ℹ️"],
        ]
    )
    
    # 2. Python Code Analysis
    print("\n🐍 Analyzing Python source...")
    if Path(args.src).exists():
        code_analyzer = PythonCodeAnalyzer(args.src)
        code_findings = code_analyzer.analyze()
        
        call_summary = [(call, len(locs)) for call, locs in code_findings.items() if not call.startswith("_")]
        call_summary.sort(key=lambda x: -x[1])
        
        report.add_table("Critical Call Analysis",
            ["Function", "Occurrences"],
            [[call, str(count)] for call, count in call_summary[:10]]
        )
    
    # 3. TS SDK Comparison
    print("\n📦 Comparing with TS SDKs...")
    comparator = TsSdkComparator(args.ts_lighter, args.ts_x10)
    sdk_comparison = comparator.compare()
    
    if sdk_comparison:
        sdk_details = []
        for sdk_name, data in sdk_comparison.items():
            if data.get("exists"):
                modules = [m["name"] for m in data.get("modules", [])]
                patterns = data.get("patterns", [])
                sdk_details.append([sdk_name, ", ".join(modules[:3]) + "...", ", ".join(patterns)])
            else:
                sdk_details.append([sdk_name, "Not found", "-"])
        
        report.add_table("TS SDK Comparison",
            ["SDK", "Key Modules", "Patterns Found"],
            sdk_details
        )
    
    # 4. Config Validation
    print("\n⚙️ Validating config...")
    if Path(args.config).exists():
        validator = ConfigValidator(args.config)
        config_issues = validator.validate()
        
        if config_issues:
            report.add_table("Config Validation",
                ["Level", "Issue"],
                [[issue.get("level", "info"), issue.get("message", issue.get("error", "Unknown"))] for issue in config_issues]
            )
    
    # Generate report
    output_report = report.generate(args.output)
    
    if not args.output:
        print("\n" + "=" * 60)
        print(output_report)
    
    print("\n✅ Audit complete!")
    
    # Summary
    errors = len(log_metrics.get("errors", []))
    warnings = len(log_metrics.get("warnings", []))
    
    if errors == 0 and warnings == 0:
        print("🎉 No issues found - bot is healthy!")
        return 0
    else:
        print(f"⚠️ Found {errors} errors and {warnings} warnings")
        return 1


if __name__ == "__main__":
    sys.exit(main())
