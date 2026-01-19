# src/application/execution/batch_manager.py
# Note: This file has been moved to application/execution/ for better organization

import asyncio
import logging

logger = logging.getLogger(__name__)


class LighterBatchManager:
    """
    Manages batching of Lighter orders to optimize API usage (Rate Limits).

    Queues signed order transactions and flushes them periodically or when full using:
    POST /api/v1/sendTxBatch
    """

    def __init__(self, adapter, batch_size: int = 20, flush_interval: float = 0.2):
        self.adapter = adapter
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self._queue_types: list[int] = []
        self._queue_infos: list[str] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """Start the background flush task."""
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info(f"📦 BatchManager started (size={self.batch_size}, interval={self.flush_interval}s)")

    async def stop(self):
        """Stop the batch manager and flush remaining items."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        # Flush remaining items
        await self.flush()
        logger.info("📦 BatchManager stopped")

    async def _flush_loop(self):
        """Background loop to flush the batch periodically."""
        while self._running:
            await asyncio.sleep(self.flush_interval)
            await self.flush()

    async def add_create_order(self, **kwargs) -> bool:
        """
        Sign and queue a CREATE_ORDER transaction.

        Args:
            **kwargs: Arguments passed to signer.sign_create_order()
                      (market_index, base_amount, price, is_ask, etc.)

        Returns:
            bool: True if queued successfully
        """
        try:
            signer = await self.adapter._get_signer()
            if not signer:
                logger.error("❌ BatchManager: No signer available")
                return False

            # Ensure types are correct (int for most params) to avoid SDK errors
            # Lighter SDK is strict about types.
            if "market_index" in kwargs:
                kwargs["market_index"] = int(kwargs["market_index"])
            if "base_amount" in kwargs:
                kwargs["base_amount"] = int(kwargs["base_amount"])
            if "price" in kwargs:
                kwargs["price"] = int(kwargs["price"])
            if "nonce" in kwargs:
                kwargs["nonce"] = int(kwargs["nonce"])
            if "client_order_index" in kwargs:
                kwargs["client_order_index"] = int(kwargs["client_order_index"])

            # Using specific TX_TYPE constant for CREATE_ORDER
            # Safer to fetch from signer instance if available
            tx_type = getattr(signer, "TX_TYPE_CREATE_ORDER", 1)  # Default to 1 if missing

            # Sign the transaction (Sync operation usually)
            # signer.sign_create_order returns the JSON string needed for sendTxBatch
            tx_info = signer.sign_create_order(**kwargs)

            if not tx_info:
                logger.error("❌ BatchManager: Failed to sign order (empty result)")
                return False

            async with self._lock:
                self._queue_types.append(int(tx_type))
                self._queue_infos.append(str(tx_info))

                # Check if we should flush immediately
                should_flush = len(self._queue_types) >= self.batch_size

            if should_flush:
                # Flush asynchronously to not block the caller
                asyncio.create_task(self.flush())

            return True

        except Exception as e:
            logger.error(f"❌ BatchManager add_create_order error: {e}")
            return False

    async def add_cancel_order(self, **kwargs) -> bool:
        """Sign and queue a CANCEL_ORDER transaction."""
        try:
            signer = await self.adapter._get_signer()
            if not signer:
                return False

            tx_type = getattr(signer, "TX_TYPE_CANCEL_ORDER", 4)  # Default to 4 (verify)

            # Enforce types
            if "market_index" in kwargs:
                kwargs["market_index"] = int(kwargs["market_index"])
            if "order_index" in kwargs:
                kwargs["order_index"] = int(kwargs["order_index"])
            if "nonce" in kwargs:
                kwargs["nonce"] = int(kwargs["nonce"])

            tx_info = signer.sign_cancel_order(**kwargs)

            async with self._lock:
                self._queue_types.append(int(tx_type))
                self._queue_infos.append(str(tx_info))
                should_flush = len(self._queue_types) >= self.batch_size

            if should_flush:
                asyncio.create_task(self.flush())

            return True
        except Exception as e:
            logger.error(f"❌ BatchManager add_cancel_order error: {e}")
            return False

    async def flush(self):
        """Send queued transactions in a single batch."""
        async with self._lock:
            if not self._queue_types:
                return

            # Take snapshot of queue
            types_to_send = list(self._queue_types)
            infos_to_send = list(self._queue_infos)

            # Clear queue
            self._queue_types.clear()
            self._queue_infos.clear()

        # Send batch via adapter
        try:
            logger.info(f"🚀 BatchManager: Sending {len(types_to_send)} txs...")
            success, hashes = await self.adapter.send_batch_orders(types_to_send, infos_to_send)

            if success:
                logger.info(f"✅ BatchManager: Batch success ({len(hashes)} hashes)")
            else:
                logger.error("❌ BatchManager: Batch submission failed!")
                # TODO: Improve error handling? Re-queue?
                # For now, we drop failed batches to avoid head-of-line blocking loops.

        except Exception as e:
            logger.error(f"❌ BatchManager flush error: {e}")
