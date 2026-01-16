"""
WebSocket Stability Tests.

Tests for the new WebSocket stability improvements:
- Circuit Breaker pattern
- Health Monitor
- Exponential Backoff with Jitter
"""

import asyncio
import time

import pytest

from funding_bot.adapters.exchanges.lighter.adapter import (
    _WebSocketCircuitBreaker,
    _WebSocketHealthMonitor,
    _get_ws_reconnect_delay,
)


# =============================================================================
# Circuit Breaker Tests
# =============================================================================


class TestWebSocketCircuitBreaker:
    """Tests for the WebSocket circuit breaker pattern."""

    def test_circuit_breaker_initial_state(self):
        """Should start with circuit closed (allowing connections)."""
        cb = _WebSocketCircuitBreaker(threshold=10, cooldown_seconds=60.0)
        assert cb.circuit_open is False
        assert cb.failure_count == 0
        assert cb.should_attempt_connection() is True

    def test_circuit_breaker_opens_after_threshold(self):
        """Should open circuit after threshold failures."""
        cb = _WebSocketCircuitBreaker(threshold=3, cooldown_seconds=60.0)

        # Record failures up to threshold
        for i in range(3):
            opened = cb.record_failure()

        # Should open on the third failure
        assert cb.circuit_open is True
        assert cb.failure_count == 3

    def test_circuit_breaker_blocks_connections_when_open(self):
        """Should block connection attempts when circuit is open."""
        cb = _WebSocketCircuitBreaker(threshold=2, cooldown_seconds=60.0)

        # Trigger circuit
        cb.record_failure()
        cb.record_failure()

        assert cb.circuit_open is True
        assert cb.should_attempt_connection() is False

    def test_circuit_breaker_resets_after_cooldown(self):
        """Should reset after cooldown period elapses."""
        cb = _WebSocketCircuitBreaker(threshold=2, cooldown_seconds=0.1)

        # Trigger circuit
        cb.record_failure()
        cb.record_failure()

        assert cb.circuit_open is True

        # Wait for cooldown
        time.sleep(0.15)

        # Should now allow connection
        assert cb.should_attempt_connection() is True
        assert cb.circuit_open is False

    def test_circuit_breaker_resets_on_success(self):
        """Should reset immediately on successful connection."""
        cb = _WebSocketCircuitBreaker(threshold=3, cooldown_seconds=60.0)

        # Trigger circuit
        for _ in range(3):
            cb.record_failure()

        assert cb.circuit_open is True

        # Simulate successful connection
        cb.record_success()

        assert cb.circuit_open is False
        assert cb.failure_count == 0

    def test_circuit_breaker_returns_true_on_threshold(self):
        """record_failure() should return True when circuit opens."""
        cb = _WebSocketCircuitBreaker(threshold=2, cooldown_seconds=60.0)

        # First failure - circuit should NOT open yet
        assert cb.record_failure() is False
        assert cb.circuit_open is False

        # Second failure - circuit SHOULD open
        assert cb.record_failure() is True
        assert cb.circuit_open is True


# =============================================================================
# Health Monitor Tests
# =============================================================================


class TestWebSocketHealthMonitor:
    """Tests for the WebSocket health monitor."""

    def test_health_monitor_initial_state(self):
        """Should start healthy with grace period."""
        hm = _WebSocketHealthMonitor(message_threshold=5, error_threshold=5)

        # Within grace period (5 seconds), should be healthy
        assert hm.is_healthy() is True
        assert hm.message_count == 0
        assert hm.error_count == 0

    def test_health_monitor_records_messages(self):
        """Should track message count and timestamps."""
        hm = _WebSocketHealthMonitor()

        hm.record_message()
        assert hm.message_count == 1

        hm.record_message()
        hm.record_message()
        assert hm.message_count == 3

    def test_health_monitor_records_errors(self):
        """Should track error count."""
        hm = _WebSocketHealthMonitor()

        hm.record_error()
        assert hm.error_count == 1

        hm.record_error()
        hm.record_error()
        assert hm.error_count == 3

    def test_health_monitor_detects_no_messages(self):
        """Should detect unhealthy when no recent messages."""
        hm = _WebSocketHealthMonitor(message_threshold=5, error_threshold=5)

        # Record initial message
        hm.record_message()

        # Simulate time passing (beyond grace period + message timeout)
        hm.connection_start = time.monotonic() - 40
        hm.last_message_time = time.monotonic() - 35

        assert hm.is_healthy() is False

    def test_health_monitor_detects_too_many_errors(self):
        """Should detect unhealthy when error threshold exceeded."""
        hm = _WebSocketHealthMonitor(message_threshold=5, error_threshold=3)

        # Simulate aged connection (past grace period)
        hm.connection_start = time.monotonic() - 10
        hm.last_message_time = time.monotonic() - 1  # Recent message

        # Record errors exceeding threshold
        for _ in range(5):
            hm.record_error()

        assert hm.is_healthy() is False

    def test_health_monitor_allows_some_errors(self):
        """Should remain healthy with errors below threshold."""
        hm = _WebSocketHealthMonitor(message_threshold=5, error_threshold=5)

        # Simulate aged connection (past grace period)
        hm.connection_start = time.monotonic() - 10
        hm.last_message_time = time.monotonic() - 1  # Recent message

        # Record errors below threshold
        for _ in range(3):
            hm.record_error()

        assert hm.is_healthy() is True


# =============================================================================
# Exponential Backoff Tests
# =============================================================================


class TestExponentialBackoff:
    """Tests for exponential backoff with jitter."""

    def test_backoff_delay_increases_exponentially(self):
        """Should increase delay exponentially with each attempt."""
        delays = [_get_ws_reconnect_delay(i, base_delay=2.0, max_delay=120.0, jitter_factor=0) for i in range(5)]

        # Without jitter, should follow exponential pattern
        assert delays[0] == 2.0   # 2 * 2^0
        assert delays[1] == 4.0   # 2 * 2^1
        assert delays[2] == 8.0   # 2 * 2^2
        assert delays[3] == 16.0  # 2 * 2^3
        assert delays[4] == 32.0  # 2 * 2^4

    def test_backoff_respects_max_delay(self):
        """Should cap delay at max_delay value."""
        max_delay = 30.0
        delays = [
            _get_ws_reconnect_delay(i, base_delay=2.0, max_delay=max_delay, jitter_factor=0)
            for i in range(10)
        ]

        # All delays should be <= max_delay
        for delay in delays:
            assert delay <= max_delay

    def test_backoff_includes_jitter(self):
        """Should add random jitter to prevent connection storms."""
        # Generate multiple delays with same attempt number
        delays = [
            _get_ws_reconnect_delay(5, base_delay=2.0, max_delay=120.0, jitter_factor=0.15)
            for _ in range(20)
        ]

        # With jitter, delays should vary
        # All should be around 64s (2 * 2^5) ± 15%
        unique_delays = set(delays)
        assert len(unique_delays) > 1  # Should have variation

        # All should be within expected bounds
        expected = 64.0  # 2 * 2^5
        for delay in delays:
            assert delay >= expected * 0.85  # -15%
            assert delay <= expected * 1.15  # +15%

    def test_backoff_never_negative(self):
        """Should never return negative delay."""
        for i in range(20):
            delay = _get_ws_reconnect_delay(i, base_delay=2.0, max_delay=120.0, jitter_factor=0.15)
            assert delay >= 0

    def test_backoff_zero_jitter_is_deterministic(self):
        """Should be deterministic when jitter_factor is 0."""
        delays = [
            _get_ws_reconnect_delay(5, base_delay=2.0, max_delay=120.0, jitter_factor=0)
            for _ in range(10)
        ]

        # All should be exactly the same
        assert len(set(delays)) == 1
        assert delays[0] == 64.0  # 2 * 2^5


# =============================================================================
# Integration Tests
# =============================================================================


class TestWebSocketStabilityIntegration:
    """Integration tests for WebSocket stability components."""

    def test_circuit_breaker_and_backoff_integration(self):
        """Should integrate circuit breaker with backoff logic."""
        cb = _WebSocketCircuitBreaker(threshold=3, cooldown_seconds=0.5)

        attempt = 0
        connection_attempts = 0

        # Simulate connection failures
        while connection_attempts < 5:
            if not cb.should_attempt_connection():
                # Circuit is open - wait for cooldown
                break

            # Simulate connection attempt
            connection_attempts += 1
            cb.record_failure()

            # Get next delay
            delay = _get_ws_reconnect_delay(attempt, base_delay=2.0, max_delay=120.0, jitter_factor=0)
            assert delay >= 0
            attempt += 1

        # Should have stopped after circuit opened (3 failures)
        assert connection_attempts == 3
        assert cb.circuit_open is True

    @pytest.mark.asyncio
    async def test_health_monitor_with_simulated_ws_messages(self):
        """Should track health through simulated message flow."""
        hm = _WebSocketHealthMonitor(message_threshold=5, error_threshold=5)

        # Simulate normal operation
        for _ in range(10):
            hm.record_message()
            await asyncio.sleep(0.01)

        # Should be healthy
        assert hm.is_healthy() is True
        assert hm.message_count == 10

        # Age the connection beyond grace period (5 seconds)
        hm.connection_start = time.monotonic() - 10
        hm.last_message_time = time.monotonic() - 1  # Recent message

        # Simulate errors exceeding threshold
        for _ in range(6):
            hm.record_error()

        # Should be unhealthy now
        assert hm.is_healthy() is False

    def test_full_stability_stack_simulation(self):
        """Should simulate full stability workflow."""
        cb = _WebSocketCircuitBreaker(threshold=10, cooldown_seconds=60.0)
        hm = _WebSocketHealthMonitor(message_threshold=5, error_threshold=5)

        # Simulate healthy connection
        for _ in range(10):
            hm.record_message()

        assert cb.should_attempt_connection() is True
        assert hm.is_healthy() is True

        # Simulate connection degradation (below threshold)
        for _ in range(5):
            hm.record_error()
            cb.record_failure()

        # Should still be allowed (below threshold)
        assert cb.should_attempt_connection() is True

        # More failures trigger circuit (total 10)
        for _ in range(5):
            hm.record_error()
            cb.record_failure()

        assert cb.circuit_open is True

        # Recovery after cooldown requires time
        # (tested in separate test above)
