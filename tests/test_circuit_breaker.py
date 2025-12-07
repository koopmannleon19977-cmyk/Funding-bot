
import pytest
from src.circuit_breaker import CircuitBreaker

def test_circuit_breaker_failure_trigger(circuit_breaker_config):
    """Test trigger after consecutive failures"""
    cb = CircuitBreaker(circuit_breaker_config)
    assert not cb.is_triggered

    # Config is set to 2 failures
    cb.record_trade_result(False, "BTC-USD")
    assert cb.failure_count == 1
    assert not cb.is_triggered

    cb.record_trade_result(False, "ETH-USD")
    assert cb.failure_count == 2
    assert cb.is_triggered
    assert "Too many consecutive" in cb._trigger_reason

def test_circuit_breaker_reset(circuit_breaker_config):
    """Test failure count reset on success"""
    cb = CircuitBreaker(circuit_breaker_config)
    
    cb.record_trade_result(False, "BTC-USD")
    assert cb.failure_count == 1
    
    cb.record_trade_result(True, "ETH-USD")
    assert cb.failure_count == 0
    assert not cb.is_triggered

def test_circuit_breaker_drawdown(circuit_breaker_config):
    """Test drawdown trigger"""
    cb = CircuitBreaker(circuit_breaker_config)
    
    # Base equity
    cb.update_equity(1000.0) # Peak
    assert not cb.is_triggered
    
    # Drop 5% (Limit is 10%)
    cb.update_equity(950.0)
    assert not cb.is_triggered
    
    # Drop 15% (Limit is 10%)
    cb.update_equity(850.0)
    assert cb.is_triggered
    assert "Critical Drawdown" in cb._trigger_reason
