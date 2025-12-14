---
description: Implement Lighter Batch Orders functionality
---

# Lighter Batch Orders Implementation

This workflow outlines the steps to implement Lighter Batch Orders to improve API efficiency.

## 1. Create Batch Manager
Create `src/batch_manager.py` to handle the queueing and aggregation of orders.
- Implement a `BatchManager` class.
- Add methods to `add_order` and `flush_batch`.
- Use `asyncio.create_task` for background flushing or explicit flush calls.

## 2. Update Lighter Adapter
Modify `src/adapters/lighter_adapter.py` to use the `BatchManager`.
- Integreate `send_batch_orders` logic (using the existing method verified in `lighter_adapter.py`).
- Replace individual `create_order` calls with batch accumulation where appropriate (e.g., during startup/rebalance).

## 3. Configuration
- Add batch size and interval settings to `src/config.py`.

## 4. Testing
- Verify that orders are correctly batched and sent as a single transaction.
- Ensure error handling works (if one order in a batch fails).
