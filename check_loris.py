import json

with open('loris_api.json') as f:
    data = json.load(f)

# Get BERA rates
lighter = data['funding_rates'].get('lighter', {}).get('BERA', 'N/A')
extended = data['funding_rates'].get('extended', {}).get('BERA', 'N/A')

print(f"Lighter BERA raw: {lighter}")
print(f"Lighter BERA %: {lighter / 10000 if lighter != 'N/A' else 'N/A'}")
print(f"Extended BERA raw: {extended}")
print(f"Extended BERA %: {extended / 10000 if extended != 'N/A' else 'N/A'}")

# Calculate spread
if lighter != 'N/A' and extended != 'N/A':
    spread = abs(lighter - extended) / 10000
    print(f"\nSpread (raw): {abs(lighter - extended)}")
    print(f"Spread %: {spread}%")
    print(f"Spread % (×8 for 8-hour equivalent): {spread * 8}%")
    print(f"\nAPY calculations:")
    print(f"  Hourly × 24 × 365: {spread * 24 * 365}%")
    print(f"  8-hourly × 3 × 365: {spread * 8 * 3 * 365}%")
