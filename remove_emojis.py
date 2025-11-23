# remove_emojis.py - Einmal ausführen, dann löschen
import os
import re

def remove_emojis_from_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Alle Emojis entfernen
    emoji_pattern = re.compile("["
        u"\U0001F600-\U0001F64F"  # emoticons
        u"\U0001F300-\U0001F5FF"  # symbols & pictographs
        u"\U0001F680-\U0001F6FF"  # transport & map symbols
        u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
        u"\U00002702-\U000027B0"
        u"\U000024C2-\U0001F251"
        u"\u2705\u2728\u2B50"      # checkmarks, stars
        u"\u26A0\u23F0\u2B06"      # warnings, clocks
        u"\u23E9\u23EA\u2B07"
        u"\u2934\u2935"
        "]+", flags=re.UNICODE)
    
    content = emoji_pattern.sub('', content)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"✓ {filepath}")

files = [
    'scripts/monitor_funding.py',
    'src/adapters/x10_adapter.py',
    'src/adapters/lighter_adapter.py',
    'src/adapters/base_adapter.py',
    'src/state_manager.py',
    'src/rate_limiter.py',
    'src/parallel_execution.py',
    'src/latency_arb.py',
    'src/prediction_v2.py',
    'config.py'
]

for f in files:
    if os.path.exists(f):
        remove_emojis_from_file(f)

print("\n✓ Alle Emojis entfernt!")