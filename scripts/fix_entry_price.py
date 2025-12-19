"""Fix Entry Price Phantom Profit Issue - v2"""
filepath = r'c:\Users\koopm\funding-bot\src\core\trading.py'

# Read file preserving line endings
with open(filepath, 'rb') as f:
    content = f.read()

# Decode
content_str = content.decode('utf-8')

# The old code block to find (with CRLF line endings \r\n)
old_block = """                entry_price_x10 = x10.fetch_mark_price(symbol) or 0.0\r
                entry_price_lighter = lighter.fetch_mark_price(symbol) or 0.0\r
                apy_value = opp.get('apy', 0.0)"""

# New code block (with CRLF line endings to match)
new_block = """                # FIX (Phantom Profit): Fetch ACTUAL fill prices from exchanges!
                entry_price_x10 = 0.0
                entry_price_lighter = 0.0
                try:
                    tasks = []
                    tasks.append(x10.get_order(str(x10_id), symbol) if x10_id else asyncio.sleep(0, result=None))
                    tasks.append(lighter.get_order(str(lit_id), symbol) if lit_id else asyncio.sleep(0, result=None))
                    x10_order, lighter_order = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    if x10_order and not isinstance(x10_order, Exception):
                        p = safe_float(x10_order.get("avgFillPrice") or x10_order.get("price") or 0.0)
                        if p > 0:
                            entry_price_x10 = p
                    if lighter_order and not isinstance(lighter_order, Exception):
                        p = safe_float(lighter_order.get("avg_price") or lighter_order.get("average_price") or lighter_order.get("price") or 0.0)
                        if p > 0:
                            entry_price_lighter = p
                    
                    if entry_price_x10 <= 0:
                        entry_price_x10 = x10.fetch_mark_price(symbol) or 0.0
                        logger.warning(f"X10 fill price not found, using mark price")
                    if entry_price_lighter <= 0:
                        entry_price_lighter = lighter.fetch_mark_price(symbol) or 0.0
                        logger.warning(f"Lighter fill price not found, using mark price")
                except Exception as e:
                    logger.warning(f"Error fetching fill prices: {e}")
                    entry_price_x10 = x10.fetch_mark_price(symbol) or 0.0
                    entry_price_lighter = lighter.fetch_mark_price(symbol) or 0.0
                
                apy_value = opp.get('apy', 0.0)"""

# Replace CRLF with LF in new block to match file style after
new_block_crlf = new_block.replace('\n', '\r\n')

if old_block in content_str:
    new_content = content_str.replace(old_block, new_block_crlf)
    with open(filepath, 'wb') as f:
        f.write(new_content.encode('utf-8'))
    print('SUCCESS: File updated with CRLF matching!')
else:
    # Try without CR
    old_block_lf = old_block.replace('\r\n', '\n').replace('\r', '')
    if old_block_lf in content_str:
        new_content = content_str.replace(old_block_lf, new_block)
        with open(filepath, 'wb') as f:
            f.write(new_content.encode('utf-8'))
        print('SUCCESS: File updated with LF matching!')
    else:
        print('ERROR: Pattern not found')
        # Debug
        for i, line in enumerate(content_str.split('\n')):
            if 'entry_price_x10 = x10.fetch_mark_price' in line:
                print(f'Line {i+1}: {repr(line[:80])}')
