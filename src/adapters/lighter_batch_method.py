async def batch_create_limit_order(
    self, symbol: str, side: str, notional_usd: float, price: float = None, reduce_only: bool = False
) -> bool:
    """
    Queue a create limit order for batched execution.
    Reusable logic from open_live_position logic for sizing/pricing.
    """
    try:
        if not self.market_info or symbol not in self.market_info:
            logger.error(f"❌ Market info missing for {symbol}")
            return False

        # Validations
        if notional_usd <= 0 and not price:
            return False

        market_info = self.market_info[symbol]
        market_id = market_info.get("i") or market_info.get("market_id") or market_info.get("market_index")
        if market_id is None:
            return False

        # Price Logic (simplified from open_live_position)
        limit_price = price
        if not limit_price:
            # Must provide price for limit order
            return False

        # Quantization
        size_inc = safe_float(market_info.get("ss", 0.0001))
        if size_inc == 0:
            size_inc = 0.0001

        price_inc = safe_float(market_info.get("ts", 0.01))
        if price_inc == 0:
            price_inc = 0.01

        raw_amount = float(notional_usd) / float(limit_price)
        quantized_size = quantize_value(raw_amount, size_inc, rounding=ROUND_FLOOR)
        quantized_price = quantize_value(float(limit_price), price_inc)

        size_decimals = int(market_info.get("sd", 8))
        price_decimals = int(market_info.get("pd", 6))
        scale_base = 10**size_decimals
        scale_price = 10**price_decimals

        base = int(round(quantized_size * scale_base))
        price_int = int(round(quantized_price * scale_price))

        if base <= 0:
            logger.error(f"❌ Batch create: Base amount 0 for {symbol}")
            return False

        # Get Nonce
        async with self.order_lock:
            nonce = await self._get_next_nonce()
            if nonce is None:
                return False

        client_oid = int(time.time() * 1000) + random.randint(0, 99999)

        # Add to Batch Manager
        if hasattr(self, "batch_manager"):
            # ORDER_TYPE_LIMIT is usually 0
            order_type_limit = getattr(SignerClient, "ORDER_TYPE_LIMIT", 0)

            # TIF: Default GTC unless specified (TODO: make params)
            # For now using GTC
            tif = getattr(SignerClient, "ORDER_TIME_IN_FORCE_GTC", 0)

            success = await self.batch_manager.add_create_order(
                market_index=int(market_id),
                client_order_index=int(client_oid),
                base_amount=int(base),
                price=int(price_int),
                is_ask=bool(side == "SELL"),
                order_type=int(order_type_limit),
                time_in_force=int(tif),
                reduce_only=bool(reduce_only),
                nonce=int(nonce),
                api_key_index=int(self._resolved_api_key_index or 0),
            )
            return success
        else:
            logger.error("❌ BatchManager not initialized")
            return False

    except Exception as e:
        logger.error(f"❌ Batch create exception: {e}")
        return False
