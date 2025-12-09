from decimal import Decimal
import pytest
from src.parallel_execution import calculate_common_quantity
from src.utils import safe_decimal, quantize_value

def test_safe_decimal_precision():
    """Test that safe_decimal handles float precision errors correctly."""
    # The classic float error
    f_val = 0.1 + 0.2
    assert f_val != 0.3 # Float behavior
    
    d_val = safe_decimal(0.1) + safe_decimal(0.2)
    assert d_val == Decimal("0.3")

def test_calculate_common_quantity_precision():
    """Test calculate_common_quantity using Decimal concepts."""
    # exact multiple
    # 256.0 USD / 1.0 Price = 256.0 coins
    # step 10
    # expected 250
    qty = calculate_common_quantity(256.0, 1.0, 1.0, 10.0)
    assert qty == 250.0  # Currently returns float
    
    # Precision case: 0.30000000004 vs 0.3
    # If we have 0.300000004 coins and step is 0.1 -> 0.3
    # If we have 0.299999999 coins and step is 0.1 -> 0.2
    
    # Case with small step
    # 100 USD, price 1.0, step 0.0001
    # 100 / 1 = 100
    qty = calculate_common_quantity(100.0, 1.0, 0.0001, 0.0001)
    assert qty == 100.0

def test_calculate_common_quantity_decimal_inputs():
    """Test what happens if we pass Decimals to the function (future proofing)."""
    qty = calculate_common_quantity(
        Decimal("256.0"), Decimal("1.0"), Decimal("1.0"), Decimal("10.0")
    )
    # The current implementation might crash or work depending on math.floor logic with Decimal
    # We want it to work and ideally return a float (for now) or Decimal (if we fully switch)
    # The plan says "Return float ONLY at the very end if required".
    # So we expect a number that equals 250
    assert float(qty) == 250.0

def test_quantize_value_precision():
    """Test quantize_value with Decimal inputs."""
    from decimal import ROUND_FLOOR
    
    # 0.1234 quantize to 0.01 with FLOOR -> 0.12
    val = quantize_value(0.129, 0.01, rounding=ROUND_FLOOR)
    assert val == 0.12
    
    # Decimal input
    val = quantize_value(Decimal("0.129"), Decimal("0.01"))
    assert val == 0.12
    
    # Weird float case: 0.2999999999999... -> 0.2 with step 0.1
    # safe_decimal("0.29999...") -> 0.29999... -> floor to 0.1 -> 0.2
    val = quantize_value(0.2999999999, 0.1)
    assert val == 0.2
