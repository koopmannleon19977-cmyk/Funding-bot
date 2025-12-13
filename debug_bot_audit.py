#!/usr/bin/env python3
"""
Debug Bot Audit Script
======================

Comprehensive audit tool for the Funding Bot codebase.
Scans Python files for SDK calls, validates config, parses logs/CSVs,
and can auto-update the audit checklist.

Usage:
    python debug_bot_audit.py --log logs/funding_bot_*.log --output audit_update.md
    python debug_bot_audit.py --scan-imports
    python debug_bot_audit.py --validate-config
    python debug_bot_audit.py --parse-logs
    python debug_bot_audit.py --full-audit

Author: Audit Bot
Version: 2.0.0
"""

import argparse
import ast
import glob
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Optional: pandas for CSV analysis
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print("⚠️ pandas not installed - CSV analysis will be limited")

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AuditConfig:
    """Configuration for the audit script"""
    project_root: Path = field(default_factory=lambda: Path(__file__).parent)
    src_dir: str = "src"
    log_dir: str = "logs"
    data_dir: str = "data"
    checklist_file: str = "AUDIT_CHECKLIST.md"
    
    # SDK patterns to scan for
    lighter_sdk_patterns: List[str] = field(default_factory=lambda: [
        "OrderApi", "FundingApi", "AccountApi", "SignerClient",
        "SaferSignerClient", "create_order", "cancel_order",
        "fetch_open_positions", "get_price", "get_funding_rate"
    ])
    
    x10_sdk_patterns: List[str] = field(default_factory=lambda: [
        "X10TradingClient", "StreamClient", "place_order", "cancel_order",
        "get_positions", "get_balance", "OrderSide", "TimeInForce"
    ])
    
    # Config validation limits (from TS SDK docs)
    config_limits: Dict[str, Any] = field(default_factory=lambda: {
        "LIGHTER_WS_MAX_SUBSCRIPTIONS": 100,
        "LIGHTER_ORDER_TIMEOUT_SECONDS": (15.0, 120.0),  # (min, max)
        "MAKER_ORDER_MAX_RETRIES": (1, 5),
        "WS_RECONNECT_DELAY_MAX": 300,
        "NONCE_CACHE_TTL": (5.0, 60.0),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# AST SCANNER - SDK CALLS AND IMPORTS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SDKCall:
    """Represents a SDK function call found in code"""
    file: str
    line: int
    sdk: str  # "lighter" or "x10"
    function: str
    context: str  # surrounding code context


class SDKScanner(ast.NodeVisitor):
    """AST visitor to find SDK imports and calls"""
    
    def __init__(self, filename: str, config: AuditConfig):
        self.filename = filename
        self.config = config
        self.imports: List[Dict[str, Any]] = []
        self.calls: List[SDKCall] = []
        self.source_lines: List[str] = []
    
    def scan_file(self, filepath: Path) -> Tuple[List[Dict], List[SDKCall]]:
        """Scan a Python file for SDK usage"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                source = f.read()
                self.source_lines = source.splitlines()
            
            tree = ast.parse(source)
            self.visit(tree)
            
        except SyntaxError as e:
            logger.warning(f"Syntax error in {filepath}: {e}")
        except Exception as e:
            logger.warning(f"Error scanning {filepath}: {e}")
        
        return self.imports, self.calls
    
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self._check_import(alias.name, node.lineno)
        self.generic_visit(node)
    
    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        for alias in node.names:
            full_name = f"{module}.{alias.name}" if module else alias.name
            self._check_import(full_name, node.lineno, from_import=True)
        self.generic_visit(node)
    
    def visit_Call(self, node: ast.Call):
        func_name = self._get_call_name(node)
        if func_name:
            self._check_call(func_name, node.lineno)
        self.generic_visit(node)
    
    def _check_import(self, name: str, lineno: int, from_import: bool = False):
        """Check if import is SDK-related"""
        sdk = None
        if "lighter" in name.lower():
            sdk = "lighter"
        elif "x10" in name.lower():
            sdk = "x10"
        
        if sdk:
            self.imports.append({
                "file": self.filename,
                "line": lineno,
                "sdk": sdk,
                "import": name,
                "type": "from" if from_import else "import"
            })
    
    def _check_call(self, name: str, lineno: int):
        """Check if function call is SDK-related"""
        # Check Lighter patterns
        for pattern in self.config.lighter_sdk_patterns:
            if pattern.lower() in name.lower():
                context = self._get_context(lineno)
                self.calls.append(SDKCall(
                    file=self.filename,
                    line=lineno,
                    sdk="lighter",
                    function=name,
                    context=context
                ))
                return
        
        # Check X10 patterns
        for pattern in self.config.x10_sdk_patterns:
            if pattern.lower() in name.lower():
                context = self._get_context(lineno)
                self.calls.append(SDKCall(
                    file=self.filename,
                    line=lineno,
                    sdk="x10",
                    function=name,
                    context=context
                ))
                return
    
    def _get_call_name(self, node: ast.Call) -> Optional[str]:
        """Extract function name from Call node"""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            parts = []
            current = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        return None
    
    def _get_context(self, lineno: int, context_lines: int = 2) -> str:
        """Get surrounding code context"""
        start = max(0, lineno - context_lines - 1)
        end = min(len(self.source_lines), lineno + context_lines)
        return "\n".join(self.source_lines[start:end])


def scan_codebase(config: AuditConfig) -> Dict[str, Any]:
    """Scan entire codebase for SDK usage"""
    results = {
        "imports": [],
        "calls": [],
        "files_scanned": 0,
        "errors": []
    }
    
    src_path = config.project_root / config.src_dir
    
    # Find all Python files
    py_files = list(src_path.rglob("*.py"))
    py_files.append(config.project_root / "config.py")
    
    for filepath in py_files:
        if filepath.exists():
            scanner = SDKScanner(str(filepath.relative_to(config.project_root)), config)
            imports, calls = scanner.scan_file(filepath)
            results["imports"].extend(imports)
            results["calls"].extend(calls)
            results["files_scanned"] += 1
    
    logger.info(f"✅ Scanned {results['files_scanned']} files")
    logger.info(f"   Found {len(results['imports'])} SDK imports")
    logger.info(f"   Found {len(results['calls'])} SDK calls")
    
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG VALIDATOR
# ═══════════════════════════════════════════════════════════════════════════════

def validate_config(config: AuditConfig) -> Dict[str, Any]:
    """Validate config.py against SDK limits and best practices"""
    results = {
        "valid": [],
        "warnings": [],
        "errors": [],
        "missing": []
    }
    
    config_path = config.project_root / "config.py"
    
    if not config_path.exists():
        results["errors"].append("config.py not found!")
        return results
    
    # Parse config.py to extract values
    config_values = {}
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Simple regex extraction for common patterns
        patterns = {
            "LIGHTER_WS_MAX_SUBSCRIPTIONS": r"LIGHTER_WS_MAX_SUBSCRIPTIONS\s*=\s*(\d+)",
            "LIGHTER_ORDER_TIMEOUT_SECONDS": r"LIGHTER_ORDER_TIMEOUT_SECONDS\s*=\s*([\d.]+)",
            "MAKER_ORDER_MAX_RETRIES": r"MAKER_ORDER_MAX_RETRIES\s*=\s*(\d+)",
            "WS_RECONNECT_DELAY_MAX": r"WS_RECONNECT_DELAY_MAX\s*=\s*(\d+)",
            "NONCE_CACHE_TTL": r"_nonce_cache_ttl.*?=\s*([\d.]+)",
            "MAX_OPEN_TRADES": r"MAX_OPEN_TRADES\s*=\s*(\d+)",
            "DESIRED_NOTIONAL_USD": r"DESIRED_NOTIONAL_USD\s*=\s*([\d.]+)",
            "MIN_APY_FILTER": r"MIN_APY_FILTER\s*=\s*([\d.]+)",
        }
        
        for name, pattern in patterns.items():
            match = re.search(pattern, content)
            if match:
                try:
                    config_values[name] = float(match.group(1))
                except ValueError:
                    config_values[name] = match.group(1)
            else:
                results["missing"].append(name)
        
        # Validate against limits
        limits = config.config_limits
        
        # Check LIGHTER_WS_MAX_SUBSCRIPTIONS
        if "LIGHTER_WS_MAX_SUBSCRIPTIONS" in config_values:
            val = config_values["LIGHTER_WS_MAX_SUBSCRIPTIONS"]
            limit = limits.get("LIGHTER_WS_MAX_SUBSCRIPTIONS", 100)
            if val > limit:
                results["errors"].append(
                    f"LIGHTER_WS_MAX_SUBSCRIPTIONS ({val}) exceeds API limit ({limit})"
                )
            else:
                results["valid"].append(f"LIGHTER_WS_MAX_SUBSCRIPTIONS = {val} ✓")
        
        # Check timeout ranges
        if "LIGHTER_ORDER_TIMEOUT_SECONDS" in config_values:
            val = config_values["LIGHTER_ORDER_TIMEOUT_SECONDS"]
            min_val, max_val = limits.get("LIGHTER_ORDER_TIMEOUT_SECONDS", (15, 120))
            if val < min_val:
                results["warnings"].append(
                    f"LIGHTER_ORDER_TIMEOUT_SECONDS ({val}s) below recommended ({min_val}s)"
                )
            elif val > max_val:
                results["warnings"].append(
                    f"LIGHTER_ORDER_TIMEOUT_SECONDS ({val}s) above recommended ({max_val}s)"
                )
            else:
                results["valid"].append(f"LIGHTER_ORDER_TIMEOUT_SECONDS = {val}s ✓")
        
        # Risk check: Exposure calculation
        if "MAX_OPEN_TRADES" in config_values and "DESIRED_NOTIONAL_USD" in config_values:
            trades = config_values["MAX_OPEN_TRADES"]
            notional = config_values["DESIRED_NOTIONAL_USD"]
            total_exposure = trades * notional
            results["valid"].append(
                f"Max Exposure: {trades} × ${notional} = ${total_exposure}"
            )
            
            if total_exposure > 500:
                results["warnings"].append(
                    f"High exposure (${total_exposure}) - ensure sufficient balance"
                )
        
    except Exception as e:
        results["errors"].append(f"Error parsing config.py: {e}")
    
    # Summary
    logger.info(f"✅ Config Validation Complete:")
    logger.info(f"   Valid: {len(results['valid'])}")
    logger.info(f"   Warnings: {len(results['warnings'])}")
    logger.info(f"   Errors: {len(results['errors'])}")
    logger.info(f"   Missing: {len(results['missing'])}")
    
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# LOG PARSER
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class LogStats:
    """Statistics extracted from log files"""
    total_lines: int = 0
    info_count: int = 0
    debug_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    
    # Timing
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    
    # Specific events
    trades_opened: int = 0
    trades_closed: int = 0
    ghost_fills: int = 0
    ws_reconnects: int = 0
    rate_limit_429s: int = 0
    shutdown_clean: bool = False
    
    # Warning breakdown
    warning_types: Dict[str, int] = field(default_factory=dict)
    
    # Performance metrics
    startup_seconds: float = 0.0
    shutdown_seconds: float = 0.0


def parse_log_file(filepath: Path) -> LogStats:
    """Parse a log file and extract statistics"""
    stats = LogStats()
    
    if not filepath.exists():
        logger.error(f"Log file not found: {filepath}")
        return stats
    
    # Regex patterns
    time_pattern = re.compile(r'^(\d{2}:\d{2}:\d{2})')
    level_pattern = re.compile(r'\[(INFO|DEBUG|WARNING|ERROR)\]')
    
    warning_categories = {
        "Fill timeout": re.compile(r'Fill timeout'),
        "Ghost fill": re.compile(r'GHOST FILL'),
        "WS 1006": re.compile(r'1006'),
        "No server ping": re.compile(r'No server ping'),
        "Cancel not confirmed": re.compile(r'cancel NOT confirmed', re.I),
        "Orderbook stale": re.compile(r'orderbook stale', re.I),
    }
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                stats.total_lines += 1
                
                # Extract timestamp
                time_match = time_pattern.match(line)
                if time_match:
                    try:
                        t = datetime.strptime(time_match.group(1), '%H:%M:%S')
                        if stats.start_time is None:
                            stats.start_time = t
                        stats.end_time = t
                    except ValueError:
                        pass
                
                # Count log levels
                level_match = level_pattern.search(line)
                if level_match:
                    level = level_match.group(1)
                    if level == "INFO":
                        stats.info_count += 1
                    elif level == "DEBUG":
                        stats.debug_count += 1
                    elif level == "WARNING":
                        stats.warning_count += 1
                        # Categorize warning
                        for cat, pattern in warning_categories.items():
                            if pattern.search(line):
                                stats.warning_types[cat] = stats.warning_types.get(cat, 0) + 1
                    elif level == "ERROR":
                        stats.error_count += 1
                
                # Specific events
                if "HEDGED TRADE START" in line:
                    stats.trades_opened += 1
                if "GHOST FILL" in line:
                    stats.ghost_fills += 1
                if "Reconnect" in line.lower() and "ws" in line.lower():
                    stats.ws_reconnects += 1
                if "429" in line:
                    stats.rate_limit_429s += 1
                if "All positions closed. Bye" in line:
                    stats.shutdown_clean = True
                if "elapsed=" in line and "Shutdown" in line:
                    match = re.search(r'elapsed=([\d.]+)s', line)
                    if match:
                        stats.shutdown_seconds = float(match.group(1))
        
        # Calculate startup time (from first line to "BOT V5 RUNNING")
        # This is approximated from timestamps
        
    except Exception as e:
        logger.error(f"Error parsing log: {e}")
    
    return stats


def analyze_logs(config: AuditConfig, log_pattern: str = "funding_bot_*.log") -> Dict[str, LogStats]:
    """Analyze all matching log files"""
    log_dir = config.project_root / config.log_dir
    results = {}
    
    log_files = list(log_dir.glob(log_pattern))
    
    if not log_files:
        logger.warning(f"No log files found matching {log_pattern}")
        return results
    
    for log_file in sorted(log_files)[-5:]:  # Last 5 logs
        stats = parse_log_file(log_file)
        results[log_file.name] = stats
        
        logger.info(f"\n📊 {log_file.name}:")
        logger.info(f"   Lines: {stats.total_lines}")
        logger.info(f"   Levels: INFO={stats.info_count}, DEBUG={stats.debug_count}, "
                   f"WARNING={stats.warning_count}, ERROR={stats.error_count}")
        logger.info(f"   Trades: opened={stats.trades_opened}")
        logger.info(f"   Ghost Fills: {stats.ghost_fills}")
        logger.info(f"   429 Rate Limits: {stats.rate_limit_429s}")
        logger.info(f"   WS Reconnects: {stats.ws_reconnects}")
        logger.info(f"   Shutdown: {'✅ Clean' if stats.shutdown_clean else '❌ Unclean'} "
                   f"({stats.shutdown_seconds:.2f}s)")
        
        if stats.warning_types:
            logger.info(f"   Warning Types: {stats.warning_types}")
    
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# CSV ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_funding_fees(config: AuditConfig) -> Dict[str, Any]:
    """Analyze funding_fees.csv"""
    results = {
        "total_records": 0,
        "symbols": [],
        "total_funding": 0.0,
        "by_symbol": {},
        "errors": []
    }
    
    csv_path = config.project_root / "funding_fees.csv"
    
    if not csv_path.exists():
        results["errors"].append("funding_fees.csv not found")
        return results
    
    if not HAS_PANDAS:
        # Fallback to basic CSV parsing
        import csv
        try:
            with open(csv_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    results["total_records"] += 1
                    symbol = row.get("symbol", "unknown")
                    if symbol not in results["symbols"]:
                        results["symbols"].append(symbol)
        except Exception as e:
            results["errors"].append(f"Error parsing CSV: {e}")
        return results
    
    try:
        df = pd.read_csv(csv_path)
        results["total_records"] = len(df)
        
        if "symbol" in df.columns:
            results["symbols"] = df["symbol"].unique().tolist()
        
        # Analyze by symbol if fee column exists
        fee_cols = [c for c in df.columns if "fee" in c.lower() or "funding" in c.lower()]
        if fee_cols and "symbol" in df.columns:
            for col in fee_cols:
                if df[col].dtype in ['float64', 'int64']:
                    by_symbol = df.groupby("symbol")[col].sum().to_dict()
                    results["by_symbol"][col] = by_symbol
                    results["total_funding"] = df[col].sum()
        
        logger.info(f"\n📊 Funding Fees Analysis:")
        logger.info(f"   Total Records: {results['total_records']}")
        logger.info(f"   Unique Symbols: {len(results['symbols'])}")
        logger.info(f"   Total Funding: ${results['total_funding']:.4f}")
        
    except Exception as e:
        results["errors"].append(f"Error analyzing CSV: {e}")
    
    return results


def analyze_trades_csv(config: AuditConfig) -> Dict[str, Any]:
    """Analyze trades.csv"""
    results = {
        "total_records": 0,
        "open_trades": 0,
        "closed_trades": 0,
        "total_pnl": 0.0,
        "by_symbol": {},
        "errors": []
    }
    
    csv_path = config.project_root / "trades.csv"
    
    if not csv_path.exists():
        results["errors"].append("trades.csv not found")
        return results
    
    if not HAS_PANDAS:
        return results
    
    try:
        df = pd.read_csv(csv_path)
        results["total_records"] = len(df)
        
        if "status" in df.columns:
            results["open_trades"] = len(df[df["status"] == "open"])
            results["closed_trades"] = len(df[df["status"] == "closed"])
        
        if "closed_pnl" in df.columns:
            results["total_pnl"] = df["closed_pnl"].sum()
        
        logger.info(f"\n📊 Trades Analysis:")
        logger.info(f"   Total Records: {results['total_records']}")
        logger.info(f"   Open: {results['open_trades']}")
        logger.info(f"   Closed: {results['closed_trades']}")
        logger.info(f"   Total PnL: ${results['total_pnl']:.4f}")
        
    except Exception as e:
        results["errors"].append(f"Error analyzing trades: {e}")
    
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# MARKDOWN GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

def generate_audit_report(
    scan_results: Dict,
    config_results: Dict,
    log_results: Dict[str, LogStats],
    csv_results: Dict
) -> str:
    """Generate a markdown audit report"""
    
    lines = [
        "# 🔍 Automated Audit Report",
        "",
        f"> Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "---",
        "",
        "## 📦 SDK Usage Summary",
        "",
        f"| SDK | Imports | Calls |",
        f"|-----|---------|-------|",
    ]
    
    # Count by SDK
    lighter_imports = sum(1 for i in scan_results.get("imports", []) if i["sdk"] == "lighter")
    x10_imports = sum(1 for i in scan_results.get("imports", []) if i["sdk"] == "x10")
    lighter_calls = sum(1 for c in scan_results.get("calls", []) if c.sdk == "lighter")
    x10_calls = sum(1 for c in scan_results.get("calls", []) if c.sdk == "x10")
    
    lines.append(f"| Lighter | {lighter_imports} | {lighter_calls} |")
    lines.append(f"| X10 | {x10_imports} | {x10_calls} |")
    lines.append("")
    
    # Config validation
    lines.extend([
        "## ⚙️ Config Validation",
        "",
    ])
    
    if config_results.get("valid"):
        lines.append("### ✅ Valid Settings")
        for v in config_results["valid"]:
            lines.append(f"- {v}")
        lines.append("")
    
    if config_results.get("warnings"):
        lines.append("### ⚠️ Warnings")
        for w in config_results["warnings"]:
            lines.append(f"- {w}")
        lines.append("")
    
    if config_results.get("errors"):
        lines.append("### ❌ Errors")
        for e in config_results["errors"]:
            lines.append(f"- {e}")
        lines.append("")
    
    # Log analysis
    lines.extend([
        "## 📊 Log Analysis",
        "",
        "| Log File | Lines | Warnings | Errors | Ghost Fills | 429s | Shutdown |",
        "|----------|-------|----------|--------|-------------|------|----------|",
    ])
    
    for name, stats in log_results.items():
        shutdown_icon = "✅" if stats.shutdown_clean else "❌"
        lines.append(
            f"| {name[:30]}... | {stats.total_lines} | {stats.warning_count} | "
            f"{stats.error_count} | {stats.ghost_fills} | {stats.rate_limit_429s} | {shutdown_icon} |"
        )
    
    lines.append("")
    
    # CSV analysis
    lines.extend([
        "## 📈 Data Analysis",
        "",
        f"- Funding Records: {csv_results.get('funding', {}).get('total_records', 'N/A')}",
        f"- Trade Records: {csv_results.get('trades', {}).get('total_records', 'N/A')}",
        f"- Total PnL: ${csv_results.get('trades', {}).get('total_pnl', 0):.4f}",
        "",
    ])
    
    # Recommendations
    lines.extend([
        "## 🎯 Recommendations",
        "",
    ])
    
    recommendations = []
    
    # Based on log analysis
    for name, stats in log_results.items():
        if stats.ghost_fills > 0:
            recommendations.append(
                f"⚠️ Ghost fills detected ({stats.ghost_fills}x) - Consider faster polling"
            )
        if stats.rate_limit_429s > 0:
            recommendations.append(
                f"🔴 Rate limit errors ({stats.rate_limit_429s}x) - Reduce request rate"
            )
        if stats.warning_count > 20:
            recommendations.append(
                f"⚠️ High warning count ({stats.warning_count}) - Review warning sources"
            )
    
    # Based on config
    if config_results.get("errors"):
        recommendations.append("🔴 Fix config errors before next run")
    
    if not recommendations:
        recommendations.append("✅ No critical issues found!")
    
    for r in recommendations:
        lines.append(f"- {r}")
    
    lines.append("")
    lines.append("---")
    lines.append(f"_End of Report_")
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive audit tool for the Funding Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python debug_bot_audit.py --full-audit
  python debug_bot_audit.py --scan-imports
  python debug_bot_audit.py --parse-logs --log "funding_bot_LEON_*.log"
  python debug_bot_audit.py --validate-config
  python debug_bot_audit.py --full-audit --output audit_report.md
        """
    )
    
    parser.add_argument("--full-audit", action="store_true",
                       help="Run complete audit (imports, config, logs, CSVs)")
    parser.add_argument("--scan-imports", action="store_true",
                       help="Scan codebase for SDK imports and calls")
    parser.add_argument("--validate-config", action="store_true",
                       help="Validate config.py against SDK limits")
    parser.add_argument("--parse-logs", action="store_true",
                       help="Parse and analyze log files")
    parser.add_argument("--analyze-csv", action="store_true",
                       help="Analyze funding_fees.csv and trades.csv")
    parser.add_argument("--log", type=str, default="funding_bot_*.log",
                       help="Log file pattern to analyze")
    parser.add_argument("--output", "-o", type=str,
                       help="Output file for audit report (markdown)")
    parser.add_argument("--project-root", type=str,
                       help="Project root directory")
    
    args = parser.parse_args()
    
    # Initialize config
    config = AuditConfig()
    if args.project_root:
        config.project_root = Path(args.project_root)
    
    print("=" * 60)
    print("🔍 FUNDING BOT AUDIT SCRIPT")
    print("=" * 60)
    print(f"Project Root: {config.project_root}")
    print()
    
    # Collect results
    scan_results = {}
    config_results = {}
    log_results = {}
    csv_results = {}
    
    # Run requested analyses
    if args.full_audit or args.scan_imports:
        print("\n📦 Scanning SDK Usage...")
        scan_results = scan_codebase(config)
    
    if args.full_audit or args.validate_config:
        print("\n⚙️ Validating Config...")
        config_results = validate_config(config)
    
    if args.full_audit or args.parse_logs:
        print("\n📊 Analyzing Logs...")
        log_results = analyze_logs(config, args.log)
    
    if args.full_audit or args.analyze_csv:
        print("\n📈 Analyzing CSVs...")
        csv_results = {
            "funding": analyze_funding_fees(config),
            "trades": analyze_trades_csv(config)
        }
    
    # Generate report if output specified
    if args.output:
        report = generate_audit_report(
            scan_results, config_results, log_results, csv_results
        )
        
        output_path = config.project_root / args.output
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        
        print(f"\n✅ Report written to: {output_path}")
    
    # Print summary
    print("\n" + "=" * 60)
    print("📋 AUDIT SUMMARY")
    print("=" * 60)
    
    if scan_results:
        print(f"   Files Scanned: {scan_results.get('files_scanned', 0)}")
        print(f"   SDK Imports: {len(scan_results.get('imports', []))}")
        print(f"   SDK Calls: {len(scan_results.get('calls', []))}")
    
    if config_results:
        print(f"   Config Valid: {len(config_results.get('valid', []))}")
        print(f"   Config Warnings: {len(config_results.get('warnings', []))}")
        print(f"   Config Errors: {len(config_results.get('errors', []))}")
    
    if log_results:
        total_warnings = sum(s.warning_count for s in log_results.values())
        total_errors = sum(s.error_count for s in log_results.values())
        print(f"   Total Log Warnings: {total_warnings}")
        print(f"   Total Log Errors: {total_errors}")
    
    print("\n✅ Audit Complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
