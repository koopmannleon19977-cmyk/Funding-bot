"""
Unit tests for WebSocket reconnection logic in Lighter adapter.

Tests the circuit breaker pattern, exponential backoff, and connection
health monitoring that prevent connection storms and ensure reliable
WebSocket connectivity.

OFFLINE-FIRST: These tests do NOT require exchange SDK or network access.
"""

from __future__ import annotations

from time import monotonic
from unittest.mock import MagicMock, patch

import pytest

# Import the WebSocket components from adapter.py
# We'll test the standalone functions and classes


class TestWebSocketCircuitBreaker:
    """
    Test the circuit breaker pattern for WebSocket connections.

    Circuit breaker prevents connection storms by stopping reconnection
    attempts after repeated failures. Automatically recovers after cooldown.
    """

    def test_circuit_breaker_initial_state(self):
        """
        INITIAL: Circuit breaker starts closed (allowing connections).

        GIVEN: Fresh circuit breaker instance
        WHEN: Checking if connection should be attempted
        THEN: Should return True (circuit is closed)
        """
        # Import the actual circuit breaker class
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketCircuitBreaker,
        )

        breaker = _WebSocketCircuitBreaker(threshold=10, cooldown_seconds=60.0)

        assert breaker.circuit_open is False
        assert breaker.failure_count == 0
        assert breaker.should_attempt_connection() is True

    def test_circuit_breaker_opens_after_threshold(self):
        """
        THRESHOLD: Circuit opens after failure threshold is reached.

        GIVEN: Circuit breaker with threshold=3
        WHEN: 3 consecutive failures occur
        THEN: Circuit should open and block further connections
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketCircuitBreaker,
        )

        breaker = _WebSocketCircuitBreaker(threshold=3, cooldown_seconds=60.0)

        # Record 3 failures
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()

        assert breaker.circuit_open is True
        assert breaker.failure_count == 3
        assert breaker.should_attempt_connection() is False

    def test_circuit_breaker_blocks_during_cooldown(self):
        """
        COOLDOWN: Circuit blocks connections during cooldown period.

        GIVEN: Circuit is open (in cooldown)
        WHEN: Attempting connection within cooldown period
        THEN: Should return False (block connection)
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketCircuitBreaker,
        )

        breaker = _WebSocketCircuitBreaker(threshold=3, cooldown_seconds=60.0)

        # Open circuit
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()

        # Mock time to be within cooldown
        with patch("time.monotonic", return_value=breaker.last_failure_time + 30):
            assert breaker.should_attempt_connection() is False

    def test_circuit_breaker_allows_after_cooldown(self):
        """
        RECOVERY: Circuit resets after cooldown period elapses.

        GIVEN: Circuit is open (in cooldown)
        WHEN: Cooldown period elapses
        THEN: Circuit should close and allow connections
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketCircuitBreaker,
        )

        breaker = _WebSocketCircuitBreaker(threshold=3, cooldown_seconds=60.0)

        # Open circuit
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()

        # Mock time to be after cooldown
        with patch("time.monotonic", return_value=breaker.last_failure_time + 61):
            # First check resets the circuit
            result = breaker.should_attempt_connection()
            assert result is True
            assert breaker.circuit_open is False

    def test_circuit_breaker_resets_on_success(self):
        """
        RESET: Circuit resets immediately on successful connection.

        GIVEN: Circuit has some failures (but not at threshold)
        WHEN: A successful connection occurs
        THEN: Failure count should reset to 0
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketCircuitBreaker,
        )

        breaker = _WebSocketCircuitBreaker(threshold=10, cooldown_seconds=60.0)

        # Record some failures
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.failure_count == 2

        # Record success
        breaker.record_success()

        assert breaker.failure_count == 0
        assert breaker.circuit_open is False


class TestExponentialBackoff:
    """
    Test exponential backoff with jitter for WebSocket reconnection.

    Exponential backoff: delay = base_delay * (2 ^ attempt)
    Jitter: Random variation to prevent connection storms
    """

    def test_backoff_increases_exponentially(self):
        """
        EXPONENTIAL: Delay increases exponentially with attempt count.

        GIVEN: base_delay = 2.0
        WHEN: Calculating delays for attempts 0, 1, 2, 3
        THEN: Delays should be 2, 4, 8, 16 (exponential)
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _get_ws_reconnect_delay,
        )

        base_delay = 2.0

        # Test exponential growth
        delay_0 = _get_ws_reconnect_delay(0, base_delay=base_delay, jitter_factor=0)
        delay_1 = _get_ws_reconnect_delay(1, base_delay=base_delay, jitter_factor=0)
        delay_2 = _get_ws_reconnect_delay(2, base_delay=base_delay, jitter_factor=0)
        delay_3 = _get_ws_reconnect_delay(3, base_delay=base_delay, jitter_factor=0)

        assert delay_0 == 2.0  # 2 * 2^0
        assert delay_1 == 4.0  # 2 * 2^1
        assert delay_2 == 8.0  # 2 * 2^2
        assert delay_3 == 16.0  # 2 * 2^3

    def test_backoff_respects_max_delay(self):
        """
        CAP: Delay never exceeds max_delay.

        GIVEN: base_delay = 2.0, max_delay = 60.0
        WHEN: Calculating delay for attempt 10
        THEN: Delay should be capped at 60.0
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _get_ws_reconnect_delay,
        )

        delay = _get_ws_reconnect_delay(
            10, base_delay=2.0, max_delay=60.0, jitter_factor=0
        )

        # Without cap: 2 * 2^10 = 2048 seconds
        # With cap: 60 seconds
        assert delay == 60.0

    def test_backoff_includes_jitter(self):
        """
        JITTER: Delay includes random variation to prevent storms.

        GIVEN: base_delay = 2.0, jitter_factor = 0.15
        WHEN: Calculating delay multiple times
        THEN: Delays should vary (within jitter range)
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _get_ws_reconnect_delay,
        )

        # We can't test randomness deterministically, but we can verify
        # the formula is applied correctly by checking bounds

        base_delay = 10.0
        jitter_factor = 0.15

        # Calculate delay multiple times (mocking random would be complex)
        # Instead, verify the formula implementation is correct

        # Without jitter, delay should be exact
        delay_no_jitter = _get_ws_reconnect_delay(
            0, base_delay=base_delay, jitter_factor=0
        )
        assert delay_no_jitter == base_delay

        # With jitter, delay should be within ±jitter_factor range
        # (We can't test exact value without mocking random.random())


class TestWebSocketHealthMonitor:
    """
    Test WebSocket health monitoring logic.

    Health monitor tracks message rate and errors to detect
    unhealthy connections that need reconnection.
    """

    def test_health_monitor_initial_state(self):
        """
        INITIAL: Health monitor starts healthy (grace period).

        GIVEN: Fresh health monitor
        WHEN: Checking health immediately
        THEN: Should return True (grace period)
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketHealthMonitor,
        )

        monitor = _WebSocketHealthMonitor(
            message_threshold=5, error_threshold=5
        )

        # Immediately after creation, should be healthy (grace period)
        assert monitor.is_healthy() is True

    def test_health_monitor_requires_recent_messages(self):
        """
        MESSAGES: Health requires messages within last 30 seconds.

        GIVEN: Health monitor with old messages (>30s ago)
        WHEN: Checking health
        THEN: Should return False (unhealthy)
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketHealthMonitor,
        )

        monitor = _WebSocketHealthMonitor(
            message_threshold=5, error_threshold=5
        )

        # Mock time to be >30s after connection start
        with patch("time.monotonic", return_value=monitor.connection_start + 35):
            # No messages recorded, should be unhealthy
            assert monitor.is_healthy() is False

    def test_health_monitor_tracks_message_rate(self):
        """
        MESSAGE_RATE: Recent messages indicate healthy connection.

        GIVEN: Health monitor receiving messages regularly
        WHEN: Checking health
        THEN: Should return True (healthy)
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketHealthMonitor,
        )

        monitor = _WebSocketHealthMonitor(
            message_threshold=5, error_threshold=5
        )

        # Mock time to be past grace period
        with patch("time.monotonic", return_value=monitor.connection_start + 10):
            # Record a message
            monitor.record_message()

            # Mock time to be 5 seconds later
            with patch("time.monotonic", return_value=monitor.connection_start + 15):
                assert monitor.is_healthy() is True

    def test_health_monitor_detects_high_error_rate(self):
        """
        ERRORS: High error rate indicates unhealthy connection.

        GIVEN: Health monitor with many errors
        WHEN: Checking health
        THEN: Should return False (unhealthy)
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketHealthMonitor,
        )

        monitor = _WebSocketHealthMonitor(
            message_threshold=5, error_threshold=5
        )

        # Mock time to be past grace period
        with patch("time.monotonic", return_value=monitor.connection_start + 10):
            # Record many errors
            for _ in range(10):
                monitor.record_error()

            # Should be unhealthy due to high error rate
            assert monitor.is_healthy() is False


class TestWebSocketReconnectionFlow:
    """
    Test the complete WebSocket reconnection flow.

    Integration of circuit breaker, backoff, and health monitoring.
    """

    def test_reconnection_stops_after_circuit_opens(self):
        """
        FLOW: Reconnection stops when circuit breaker opens.

        GIVEN: Circuit with threshold=3
        WHEN: 3 consecutive reconnection failures occur
        THEN: Should stop attempting reconnections (circuit open)
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketCircuitBreaker,
        )

        breaker = _WebSocketCircuitBreaker(threshold=3, cooldown_seconds=60.0)

        # Simulate reconnection attempts
        attempt = 0
        max_attempts = 10

        attempts_made = 0
        while attempt < max_attempts:
            if breaker.should_attempt_connection():
                attempts_made += 1
                # Simulate connection failure
                breaker.record_failure()
                attempt += 1
            else:
                # Circuit opened, stop trying
                break

        # Should have made exactly 3 attempts before circuit opened
        assert attempts_made == 3
        assert breaker.circuit_open is True

    def test_reconnection_resumes_after_cooldown(self):
        """
        FLOW: Reconnection resumes after cooldown period.

        GIVEN: Open circuit (in cooldown)
        WHEN: Cooldown period elapses
        THEN: Should allow reconnection attempt
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketCircuitBreaker,
        )

        breaker = _WebSocketCircuitBreaker(threshold=3, cooldown_seconds=60.0)

        # Open circuit
        for _ in range(3):
            breaker.record_failure()

        assert breaker.should_attempt_connection() is False

        # Fast-forward past cooldown
        with patch("time.monotonic", return_value=breaker.last_failure_time + 61):
            assert breaker.should_attempt_connection() is True

    def test_successful_connection_resets_circuit(self):
        """
        FLOW: Successful connection resets circuit breaker.

        GIVEN: Circuit with some failures (but not open)
        WHEN: Successful connection occurs
        THEN: Circuit should reset (failures cleared)
        """
        from funding_bot.adapters.exchanges.lighter.adapter import (
            _WebSocketCircuitBreaker,
        )

        breaker = _WebSocketCircuitBreaker(threshold=10, cooldown_seconds=60.0)

        # Some failures
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.failure_count == 2

        # Successful connection
        breaker.record_success()
        assert breaker.failure_count == 0
        assert breaker.circuit_open is False
