"""Clean up trading.py line endings and blank lines"""

filepath = r"c:\Users\koopm\funding-bot\src\core\trading.py"

# Read file
with open(filepath, encoding="utf-8", errors="replace") as f:
    content = f.read()

# First normalize all line endings
content = content.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")

# Remove blank lines that are just whitespace
lines = content.split("\n")
cleaned_lines = []
prev_blank = False
for line in lines:
    is_blank = line.strip() == ""
    # Remove consecutive blank lines (keep max 1)
    if is_blank and prev_blank:
        continue
    cleaned_lines.append(line)
    prev_blank = is_blank

content = "\n".join(cleaned_lines)

# Write back with Unix line endings
with open(filepath, "w", encoding="utf-8", newline="\n") as f:
    f.write(content)

print(f"File cleaned: {len(cleaned_lines)} lines")
