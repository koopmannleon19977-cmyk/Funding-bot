#!/usr/bin/env python3
"""Fix indentation in parallel_execution.py for the wait_more block."""
import sys

with open('src/parallel_execution.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the line with "if wait_more:" (should be line 1743, index 1742)
wait_more_line_idx = None
for i, line in enumerate(lines):
    if 'if wait_more:' in line and i > 1700 and i < 1750:
        wait_more_line_idx = i
        break

if wait_more_line_idx is None:
    sys.stderr.write("Could not find 'if wait_more:' line\n")
    exit(1)

sys.stderr.write(f"Found 'if wait_more:' at line {wait_more_line_idx + 1}\n")

# Get the indentation of the if statement (should be 24 spaces based on line 1743)
if_indent = len(lines[wait_more_line_idx]) - len(lines[wait_more_line_idx].lstrip())
sys.stderr.write(f"if_indent = {if_indent} spaces\n")

# Find the end of the wait_more block by looking for a line at same/less indentation
# that's not empty and not a comment continuation
block_end_idx = None
for i in range(wait_more_line_idx + 1, min(wait_more_line_idx + 250, len(lines))):
    line = lines[i]
    stripped = line.lstrip()
    
    # Skip empty lines
    if not stripped:
        continue
        
    current_indent = len(line) - len(stripped)
    
    # Check if we're back at or before the if statement's level
    if current_indent <= if_indent:
        block_end_idx = i
        sys.stderr.write(f"Block ends at line {i + 1}\n")
        break

if block_end_idx is None:
    sys.stderr.write("Could not find end of wait_more block\n")
    exit(1)

# Now fix indentation: all lines from wait_more_line_idx+1 to block_end_idx-1
# should be indented by 4 more spaces
fixed_lines = lines[:wait_more_line_idx + 1]  # Keep everything up to and including "if wait_more:"

for i in range(wait_more_line_idx + 1, block_end_idx):
    line = lines[i]
    if line.strip():  # Only indent non-empty lines
        fixed_lines.append('    ' + line)
    else:
        fixed_lines.append(line)  # Keep empty lines as-is

# Add the rest
fixed_lines.extend(lines[block_end_idx:])

# Write back
with open('src/parallel_execution.py', 'w', encoding='utf-8') as f:
    f.writelines(fixed_lines)

sys.stderr.write(f"Fixed indentation for {block_end_idx - wait_more_line_idx - 1} lines\n")
