# fix_unicode.py
import re

def fix_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Ersetze → mit ->
    content = content.replace('→', '->')
    
    # Entferne alle Unicode-Symbole
    content = re.sub(r'[^\x00-\x7F]+', '', content)
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"Fixed: {path}")

files = [
    'src/rate_limiter.py',
    'src/adapters/x10_adapter.py',
    'src/adapters/lighter_adapter.py',
    'scripts/monitor_funding.py'
]

for f in files:
    try:
        fix_file(f)
    except Exception as e:
        print(f"Error {f}: {e}")

print("\nDone!")