"""
Unit tests for cumulative fill delta tracking in close operations.

This test file validates that the _delta_from_cumulative_fill helper function
correctly handles cumulative filled_qty and fee values from the exchange,
ensuring that double-counting is prevented when orders are polled multiple times.

This is critical for correct VWAP and fee accounting, especially in grid-step
close operations where multiple price levels are used.
"""

from decimal import Decimal

import pytest

from funding_bot.services.positions.close import _delta_from_cumulative_fill

pytestmark = pytest.mark.unit


def D(x: str) -> Decimal:
    """Helper to create Decimal from string."""
    return Decimal(x)


def test_delta_from_cumulative_fill_two_updates_no_double_count():
    """
    Test that cumulative values are converted to deltas correctly.

    Simulates two polling cycles for the same order:
    - Poll 1: cum_qty=50, cum_fee=0.01 at 2.1306
    - Poll 2: cum_qty=141, cum_fee=0.03 at 2.1455

    Should result in:
    - First poll: delta=50 qty, 0.01 fee
    - Second poll: delta=91 qty (not 141!), 0.02 fee (not 0.03!)
    """
    qty_seen = {}
    fee_seen = {}
    order_id = "OID-1"

    # Poll 1: cum 50 qty, fee 0.01 at 2.1306
    d_qty1, d_notional1, d_fee1 = _delta_from_cumulative_fill(
        order_id=order_id,
        cum_qty=D("50"),
        cum_fee=D("0.01"),
        fill_price=D("2.1306"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty1 == D("50")
    assert d_fee1 == D("0.01")
    assert d_notional1 == D("50") * D("2.1306")

    # Poll 2: cum 141 qty, fee 0.03 at 2.1455 (=> delta 91 qty, 0.02 fee)
    d_qty2, d_notional2, d_fee2 = _delta_from_cumulative_fill(
        order_id=order_id,
        cum_qty=D("141"),
        cum_fee=D("0.03"),
        fill_price=D("2.1455"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty2 == D("91"), f"Expected delta_qty=91, got {d_qty2}"
    assert d_fee2 == D("0.02"), f"Expected delta_fee=0.02, got {d_fee2}"
    assert d_notional2 == D("91") * D("2.1455")

    # Poll 3: same cumulative again => zero deltas (no double count)
    d_qty3, d_notional3, d_fee3 = _delta_from_cumulative_fill(
        order_id=order_id,
        cum_qty=D("141"),
        cum_fee=D("0.03"),
        fill_price=D("2.1455"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty3 == D("0"), f"Expected delta_qty=0 on repeat poll, got {d_qty3}"
    assert d_fee3 == D("0"), f"Expected delta_fee=0 on repeat poll, got {d_fee3}"
    assert d_notional3 == D("0"), f"Expected delta_notional=0 on repeat poll, got {d_notional3}"


def test_delta_from_cumulative_fill_vwap_matches_expected():
    """
    Test that VWAP calculation matches expected value for IP-style fill pattern.

    Simulates the IP case with two fills at different prices:
    - 49.89 @ 2.1306
    - 91.11 @ 2.1455

    Expected VWAP should be: (49.89*2.1306 + 91.11*2.1455) / 141 ≈ 2.14023
    """
    qty_seen = {}
    fee_seen = {}
    order_id = "OID-2"

    # Poll 1: cum_qty=49.89 at 2.1306
    d_qty1, d_notional1, d_fee1 = _delta_from_cumulative_fill(
        order_id=order_id,
        cum_qty=D("49.89"),
        cum_fee=D("0.01"),
        fill_price=D("2.1306"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    # Poll 2: cum_qty=141.00 at 2.1455 (delta = 91.11)
    d_qty2, d_notional2, d_fee2 = _delta_from_cumulative_fill(
        order_id=order_id,
        cum_qty=D("141.00"),
        cum_fee=D("0.03"),
        fill_price=D("2.1455"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    total_qty = d_qty1 + d_qty2
    total_notional = d_notional1 + d_notional2

    assert total_qty == D("141.00"), f"Expected total_qty=141.00, got {total_qty}"

    vwap = total_notional / total_qty

    # Expected VWAP:
    # (49.89 * 2.1306 + 91.11 * 2.1455) / 141.00
    # = (106.290834 + 195.503505) / 141.00
    # = 301.794339 / 141.00
    # ≈ 2.14023
    expected = (D("49.89") * D("2.1306") + D("91.11") * D("2.1455")) / D("141.00")

    # Use a tolerance; Decimals can be exact but we keep it robust
    assert abs(vwap - expected) < D("0.0000001"), f"Expected VWAP≈{expected:.6f}, got {vwap:.6f}"


def test_delta_from_cumulative_fill_guards_negative_resets():
    """
    Test that negative deltas are guarded against.

    If, exchange API returns decreasing cumulative values (due to resets
    or out-of-order updates), we should not produce negative deltas.
    """
    qty_seen = {}
    fee_seen = {}
    order_id = "OID-3"

    # First update: qty=10, fee=0.005
    d_qty1, d_notional1, d_fee1 = _delta_from_cumulative_fill(
        order_id=order_id,
        cum_qty=D("10"),
        cum_fee=D("0.005"),
        fill_price=D("2.0"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty1 == D("10")
    assert d_fee1 == D("0.005")

    # Second update: cum_qty goes BACKWARDS to 5 (API reset/out-of-order)
    # -> should not produce negative deltas
    d_qty2, d_notional2, d_fee2 = _delta_from_cumulative_fill(
        order_id=order_id,
        cum_qty=D("5"),
        cum_fee=D("0.002"),
        fill_price=D("2.0"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty2 == D("0"), f"Expected delta_qty=0 for backwards update, got {d_qty2}"
    assert d_fee2 == D("0"), f"Expected delta_fee=0 for backwards update, got {d_fee2}"
    assert d_notional2 == D("0"), f"Expected delta_notional=0 for backwards update, got {d_notional2}"

    # Third update: cum_qty returns to normal (10)
    # -> should produce delta=5 (10 - 5, not 10 - 10!)
    d_qty3, d_notional3, d_fee3 = _delta_from_cumulative_fill(
        order_id=order_id,
        cum_qty=D("10"),
        cum_fee=D("0.005"),
        fill_price=D("2.0"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    # Since we guard against negative deltas, seen value should have stayed at 10
    # So 10 - 10 = 0 delta (no double counting even after reset recovery)
    assert d_qty3 == D("0"), f"Expected delta_qty=0 after reset recovery, got {d_qty3}"
    assert d_fee3 == D("0"), f"Expected delta_fee=0 after reset recovery, got {d_fee3}"


def test_delta_from_cumulative_fill_multiple_orders():
    """
    Test that tracking works correctly across multiple different orders.
    """
    qty_seen = {}
    fee_seen = {}

    # Order 1: fills completely in one poll
    d_qty1_a, d_notional1_a, d_fee1_a = _delta_from_cumulative_fill(
        order_id="ORDER-1",
        cum_qty=D("100"),
        cum_fee=D("0.02"),
        fill_price=D("1.5"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty1_a == D("100")
    assert d_fee1_a == D("0.02")

    # Order 2: fills partially in first poll
    d_qty2_a, d_notional2_a, d_fee2_a = _delta_from_cumulative_fill(
        order_id="ORDER-2",
        cum_qty=D("50"),
        cum_fee=D("0.01"),
        fill_price=D("2.0"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty2_a == D("50")
    assert d_fee2_a == D("0.01")

    # Order 2: fills completely in second poll
    d_qty2_b, d_notional2_b, d_fee2_b = _delta_from_cumulative_fill(
        order_id="ORDER-2",
        cum_qty=D("100"),
        cum_fee=D("0.02"),
        fill_price=D("2.0"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty2_b == D("50")
    assert d_fee2_b == D("0.01")

    # Order 1: poll again (should produce zero deltas)
    d_qty1_b, d_notional1_b, d_fee1_b = _delta_from_cumulative_fill(
        order_id="ORDER-1",
        cum_qty=D("100"),
        cum_fee=D("0.02"),
        fill_price=D("1.5"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty1_b == D("0")
    assert d_fee1_b == D("0")

    # Verify totals per order
    # Order 1: 100 qty, 0.02 fee (no double count)
    # Order 2: 100 qty, 0.02 fee (50 + 50, 0.01 + 0.01)


def test_delta_from_cumulative_fill_zero_initial_values():
    """
    Test that zero initial cumulative values work correctly.
    """
    qty_seen = {}
    fee_seen = {}

    # First poll with zero values
    d_qty1, d_notional1, d_fee1 = _delta_from_cumulative_fill(
        order_id="ORDER-ZERO",
        cum_qty=D("0"),
        cum_fee=D("0"),
        fill_price=D("1.0"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty1 == D("0")
    assert d_notional1 == D("0")
    assert d_fee1 == D("0")

    # Second poll with actual fill
    d_qty2, d_notional2, d_fee2 = _delta_from_cumulative_fill(
        order_id="ORDER-ZERO",
        cum_qty=D("75"),
        cum_fee=D("0.015"),
        fill_price=D("1.0"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty2 == D("75")
    assert d_notional2 == D("75")
    assert d_fee2 == D("0.015")


def test_delta_from_cumulative_fill_high_precision():
    """
    Test that function works with high-precision Decimals.
    """
    qty_seen = {}
    fee_seen = {}

    # High-precision fill
    d_qty1, d_notional1, d_fee1 = _delta_from_cumulative_fill(
        order_id="ORDER-PRECISION",
        cum_qty=D("123.45678901"),
        cum_fee=D("0.123456789"),
        fill_price=D("2.123456789"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    assert d_qty1 == D("123.45678901")
    assert d_notional1 == D("123.45678901") * D("2.123456789")
    assert d_fee1 == D("0.123456789")

    # Additional high-precision fill
    d_qty2, d_notional2, d_fee2 = _delta_from_cumulative_fill(
        order_id="ORDER-PRECISION",
        cum_qty=D("234.56789012"),
        cum_fee=D("0.234567890"),
        fill_price=D("2.234567890"),
        qty_seen=qty_seen,
        fee_seen=fee_seen,
    )

    expected_delta_qty = D("234.56789012") - D("123.45678901")
    assert d_qty2 == expected_delta_qty
    assert d_fee2 == D("0.234567890") - D("0.123456789")
