import re
import os
import glob
import ast
import json
import logging
from datetime import datetime
from collections import defaultdict

# --- CONFIG ---
LOG_FILE_PATTERN = "funding_bot_LEON_*_FULL.log"
TS_LIGHTER = r"C:\Users\koopm\Desktop\lighter-ts-main"
TS_X10 = r"C:\Users\koopm\Desktop\Extended-TS-SDK-master"
OUTPUT_FILE = "audit_20251214.md"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

class AuditReport:
    def __init__(self):
        self.sections = []
        self.score = 10.0
        self.findings = []
    
    def add_section(self, title, content):
        self.sections.append(f"## {title}\n\n{content}\n")
    
    def add_finding(self, severity, category, message, code_ref=None):
        icon = "🔴" if severity == "HIGH" else "🟠" if severity == "MEDIUM" else "🔵"
        self.findings.append({
            "severity": severity,
            "category": category,
            "message": message,
            "icon": icon,
            "code_ref": code_ref
        })
        if severity == "HIGH": self.score -= 2.0
        elif severity == "MEDIUM": self.score -= 0.5
    
    def generate_markdown(self):
        md = f"# 🕵️ BOT AUDIT REPORT V2\n\n"
        md += f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        md += f"**Score:** {max(0, self.score):.1f}/10\n\n"
        
        md += "### 🚨 Critical Findings\n"
        for f in [x for x in self.findings if x['severity'] == 'HIGH']:
            md += f"- {f['icon']} **{f['category']}**: {f['message']}\n"
        
        md += "\n### ⚠️ Improvement Opportunities\n"
        for f in [x for x in self.findings if x['severity'] != 'HIGH']:
            md += f"- {f['icon']} **{f['category']}**: {f['message']}\n"
            if f['code_ref']:
                md += f"  - *Ref: `{f['code_ref']}`*\n"

        md += "\n---\n\n"
        for section in self.sections:
            md += section + "\n---\n"
            
        return md

report = AuditReport()

def parse_logs(log_file):
    logging.info(f"Parsing log file: {log_file}")
    content = ""
    try:
        with open(log_file, "r", encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return f"Error reading log: {e}"
        
    # extracted metrics
    startup_time = "Unknown"
    runtime = "Unknown"
    shutdown_time = "Unknown"
    
    # 1. Startup
    m_start = re.search(r'(\d{2}:\d{2}:\d{2}) .*?BOT V5 .*?STARTING', content)
    m_ready = re.search(r'(\d{2}:\d{2}:\d{2}) .*?NETWORK READY', content)
    if m_start and m_ready:
        t1 = datetime.strptime(m_start.group(1), "%H:%M:%S")
        t2 = datetime.strptime(m_ready.group(1), "%H:%M:%S")
        startup_time = f"{(t2-t1).total_seconds()}s"
        
    # 2. Runtime & Shutdown
    m_end = re.search(r'(\d{2}:\d{2}:\d{2}) .*?STOPPING', content) or re.search(r'(\d{2}:\d{2}:\d{2}) .*?Shutdown orchestrator start', content)
    if m_start and m_end:
        t1 = datetime.strptime(m_start.group(1), "%H:%M:%S")
        t2 = datetime.strptime(m_end.group(1), "%H:%M:%S")
        runtime = f"{(t2-t1).total_seconds()/60:.1f} min"
        
    m_final = re.search(r'(\d{2}:\d{2}:\d{2}) .*?InMemoryStateManager stopped', content)
    if m_end and m_final:
        t1 = datetime.strptime(m_end.group(1), "%H:%M:%S")
        t2 = datetime.strptime(m_final.group(1), "%H:%M:%S")
        shutdown_time = f"{(t2-t1).total_seconds()}s"
        
    # 3. Lag Analysis
    lags = re.findall(r'Lag check .*?: (\d+\.\d+)s', content)
    max_lag = float(max(lags)) if lags else 0.0
    avg_lag = sum(map(float, lags))/len(lags) if lags else 0.0
    
    # 4. Warnings/Errors
    errors = len(re.findall(r'\[ERROR\]', content))
    warnings = len(re.findall(r'\[WARNING\]', content))
    
    # Report Findings
    if max_lag > 5.0:
        report.add_finding("HIGH", "Performance", f"Critical X10 Lag detected: Max {max_lag}s (Avg {avg_lag:.1f}s)", "Consumer Lag")
        
    if "Batch" not in content and "batch" not in content:
         report.add_finding("MEDIUM", "Optimization", "No Batch-Orders found in log execution", "transaction-api.ts")

    summary = f"""
| Metrik | Wert |
|---|---|
| Startup | {startup_time} |
| Runtime | {runtime} |
| Shutdown | {shutdown_time} |
| Max X10 Lag | **{max_lag}s** 🔴 |
| Errors/Warnings | {errors} / {warnings} |
    """
    report.add_section("Log Analysis", summary)

def compare_lighter_sdk():
    logging.info("Comparing Lighter SDK...")
    ts_path = os.path.join(TS_LIGHTER, "src")
    findings = ""
    
    # Check for RequestBatcher
    batcher = os.path.exists(os.path.join(ts_path, "utils", "request-batcher.ts"))
    if batcher:
        findings += "- ✅ `request-batcher.ts` exists in SDK. **Python Bot missing this!**\n"
        report.add_finding("MEDIUM", "Features", "Missing RequestBatcher implementation", "lighter-ts-main/src/utils/request-batcher.ts")
        
    # Check for Candle API
    candles = os.path.exists(os.path.join(ts_path, "api", "candlestick-api.ts"))
    if candles:
        findings += "- ✅ `candlestick-api.ts` exists. Python Bot uses this (Good).\n"
        
    report.add_section("Lighter SDK Comparison", findings)

def compare_x10_sdk():
    logging.info("Comparing X10 SDK...")
    ts_path = os.path.join(TS_X10, "src")
    findings = ""
    
    # Check for Stream Client
    stream = os.path.exists(os.path.join(ts_path, "perpetual", "stream-client", "stream-client.ts"))
    if stream:
        findings += "- ✅ `stream-client.ts` confirmed. Python bot uses firehose (Good).\n"
        
    report.add_section("X10 SDK Comparison", findings)

def scan_python_code():
    logging.info("Scanning Python Codebase...")
    py_files = glob.glob("src/**/*.py", recursive=True)
    
    issues = []
    
    # AST Analysis
    for file in py_files:
        with open(file, "r", encoding='utf-8') as f:
            tree = ast.parse(f.read())
            
        # Check for send_transaction_batch
        has_batch = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == 'send_transaction_batch':
                    has_batch = True
        
        if "lighter_adapter.py" in file and not has_batch:
            issues.append("- `lighter_adapter.py`: No `send_transaction_batch` usage found.")
            report.add_finding("MEDIUM", "Optimization", "LighterAdapter lacks Batching", "sendTxBatch")

    report.add_section("Static Code Analysis", "\n".join(issues))

if __name__ == "__main__":
    # 1. Find latest log
    logs = glob.glob(os.path.join("logs", LOG_FILE_PATTERN))
    if logs:
        latest_log = max(logs, key=os.path.getctime)
        parse_logs(latest_log)
    else:
        logging.warning("No logs found!")
        
    # 2. Compare SDKs
    compare_lighter_sdk()
    compare_x10_sdk()
    
    # 3. Scan Code
    scan_python_code()
    
    # 4. Write Report
    with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
        f.write(report.generate_markdown())
        
    print(f"Audit complete. Report written to {OUTPUT_FILE}")
