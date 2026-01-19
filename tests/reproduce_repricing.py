from dataclasses import dataclass
from decimal import Decimal


@dataclass
class MockMarketInfo:
    tick_size: Decimal = Decimal("0.01")


@dataclass
class MockOrderbook:
    lighter_bid: Decimal
    lighter_ask: Decimal


class MockMarketData:
    def __init__(self, bid, ask):
        self.ob = MockOrderbook(bid, ask)

    def get_orderbook(self, symbol):
        return self.ob

    def get_market_info(self, symbol, exchange):
        return MockMarketInfo()

    def get_price(self, symbol):
        # Fallback price data
        @dataclass
        class Price:
            lighter_price: Decimal

        return Price((self.ob.lighter_bid + self.ob.lighter_ask) / 2)


# Replicating the logic to be implemented for testing purposes
# This allows us to TDD the logic before pasting it into the main file
def calculate_price_dynamic(attempt, max_attempts, side, ob, tick_size):
    # Logic to be implemented:
    # Target Price = Start Price + (Gap * Aggressiveness)

    # 0. Safety for max_attempts
    if max_attempts < 1:
        max_attempts = 1

    # 1. Calculate Aggressiveness (0.0 to 1.0)
    # If max_attempts is 1, aggressiveness is 1.0 immediately? Or 0.0?
    # Let's say if max_attempts=1, we probably want 0.0 (Passive) or maybe just best effort.
    # Standard: 0/2=0, 1/2=0.5, 2/2=1.0
    if max_attempts > 1:
        aggressiveness = Decimal(attempt) / Decimal(max_attempts - 1)
    else:
        aggressiveness = Decimal("0")  # Default to passive if only 1 attempt

    print(f"Attempt {attempt}/{max_attempts} Aggressiveness: {aggressiveness:.2f}")

    if side == "BUY":
        best_bid = ob.lighter_bid
        best_ask = ob.lighter_ask

        # Gap is distance to cross spread
        # But we want to remain MAKER. So we can surely go up to Ask - tick.
        # But "Start Price" should be Best Bid.

        target_max = best_ask - tick_size
        start_price = best_bid

        # If spread is crossed or locked
        if start_price >= target_max:
            return start_price

        gap = target_max - start_price

        # Price = Start + Gap * Aggr
        raw_price = start_price + (gap * aggressiveness)

        # Round down to tick (for buy, floor ensures we don't accidentally round up to ask?)
        # Actually standard rounding is fine, but let's floor to be safe on buy?
        # Usually (price // tick) * tick is floor.
        final_price = (raw_price // tick_size) * tick_size

        return final_price

    else:  # SELL
        best_bid = ob.lighter_bid
        best_ask = ob.lighter_ask

        target_min = best_bid + tick_size
        start_price = best_ask

        if start_price <= target_min:
            return start_price

        # Gap is negative direction
        gap = target_min - start_price  # e.g. 100 - 110 = -10

        raw_price = start_price + (gap * aggressiveness)

        # Round up for sell? (ceil)
        # (price // tick) * tick is floor.
        # If we floor 109.99 -> 109.99.
        # If we have 100.005 -> 100.00.
        # Ideally we want nearest tick.
        # Let's use standard quantized rounding.
        final_price = (raw_price / tick_size).quantize(Decimal("1")) * tick_size

        return final_price


def test_repricing():
    bid = Decimal("100.00")
    ask = Decimal("110.00")
    mid = (bid + ask) / 2
    ob = MockMarketData(bid, ask).ob

    print(f"Spread: {bid} - {ask} (Mid: {mid})")
    print("-" * 20)

    print("BUY SIDE (3 Attempts)")
    for i in range(3):
        p = calculate_price_dynamic(i, 3, "BUY", ob, Decimal("0.01"))
        print(f"Attempt {i}: {p:.2f}")

    print("-" * 20)
    print("SELL SIDE (3 Attempts)")
    for i in range(3):
        p = calculate_price_dynamic(i, 3, "SELL", ob, Decimal("0.01"))
        print(f"Attempt {i}: {p:.2f}")

    print("-" * 20)
    print("BUY SIDE (5 Attempts)")
    for i in range(5):
        p = calculate_price_dynamic(i, 5, "BUY", ob, Decimal("0.01"))
        print(f"Attempt {i}: {p:.2f}")


if __name__ == "__main__":
    test_repricing()
