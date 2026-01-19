"""
REST API Connection Pool with Batching and Rate Limiting.

🚀 PERFORMANCE: Centralized HTTP client with:
- Connection pooling for reduced latency
- Request batching for throughput
- Automatic retries with exponential backoff
- Rate limiting to prevent API throttling
- Circuit breaker for failing endpoints

Usage:
    pool = RESTAPIPool(max_concurrent=10)

    # Single request
    response = await pool.fetch("GET", "https://api.example.com/data")

    # Batch multiple requests
    responses = await pool.batch_fetch([
        ("GET", "https://api.example.com/data1"),
        ("GET", "https://api.example.com/data2"),
    ])
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


class RESTAPIPool:
    """
    High-performance REST API connection pool with batching and rate limiting.

    Features:
    - Connection pooling (persistent HTTP connections)
    - Concurrent request limiting (semaphore)
    - Request batching (group multiple requests)
    - Automatic retries with exponential backoff
    - Per-host rate limiting
    - Circuit breaker for failing endpoints
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        connection_limit: int = 100,
        connection_limit_per_host: int = 20,
        ttl_dns_cache: int = 300,
        enable_circuit_breaker: bool = True,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: float = 60.0,
    ):
        """
        Initialize REST API pool.

        Args:
            max_concurrent: Maximum concurrent requests
            connection_limit: Total connection pool size
            connection_limit_per_host: Connections per host
            ttl_dns_cache: DNS cache TTL in seconds
            enable_circuit_breaker: Enable circuit breaker
            circuit_breaker_threshold: Failures before opening circuit
            circuit_breaker_timeout: Seconds before retrying after circuit open
        """
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

        # Circuit breaker state
        self._enable_circuit_breaker = enable_circuit_breaker
        self._circuit_breaker_threshold = circuit_breaker_threshold
        self._circuit_breaker_timeout = circuit_breaker_timeout
        self._circuit_state: dict[str, dict] = {}  # host -> {failures, last_failure, open_until}

        # Rate limiting per host
        self._rate_limits: dict[str, float] = {}  # host -> min_seconds_between_requests
        self._last_request_time: dict[str, float] = {}  # host -> last_request_timestamp

        # Statistics
        self._stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "retried_requests": 0,
            "circuit_breaker_trips": 0,
        }

        self._session: aiohttp.ClientSession | None = None
        self._connector = aiohttp.TCPConnector(
            limit=connection_limit,
            limit_per_host=connection_limit_per_host,
            ttl_dns_cache=ttl_dns_cache,
            enable_cleanup_closed=True,
        )

    async def initialize(self) -> None:
        """Initialize the HTTP session."""
        if self._session is None:
            timeout = aiohttp.ClientTimeout(
                total=30,  # 30s total timeout
                connect=10,  # 10s connection timeout
                sock_read=20,  # 20s read timeout
            )
            self._session = aiohttp.ClientSession(
                connector=self._connector,
                timeout=timeout,
            )
            logger.info(f"REST API pool initialized: max_concurrent={self._max_concurrent}")

    async def close(self) -> None:
        """Close the HTTP session and cleanup resources."""
        if self._session:
            await self._session.close()
            self._session = None
            logger.info("REST API pool closed")

    def set_rate_limit(self, host: str, min_seconds_between_requests: float) -> None:
        """
        Set rate limit for a specific host.

        Args:
            host: Hostname (e.g., "api.example.com")
            min_seconds_between_requests: Minimum seconds between requests
        """
        self._rate_limits[host] = min_seconds_between_requests
        logger.debug(f"Rate limit set for {host}: {min_seconds_between_requests}s")

    async def fetch(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: Any | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> dict[str, Any] | None:
        """
        Fetch a single URL with retry logic and circuit breaker.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: URL to fetch
            headers: Optional headers
            params: Optional query parameters
            json: Optional JSON body
            data: Optional raw data body
            max_retries: Maximum retry attempts
            retry_base_delay: Base delay for exponential backoff

        Returns:
            JSON response or None on failure
        """
        await self.initialize()

        # Extract host for rate limiting and circuit breaker
        host = self._extract_host(url)

        # Check circuit breaker
        if self._is_circuit_open(host):
            logger.warning(f"Circuit breaker open for {host}, skipping request")
            self._stats["failed_requests"] += 1
            return None

        # Apply rate limiting
        await self._wait_for_rate_limit(host)

        # Execute request with retries
        for attempt in range(max_retries + 1):
            try:
                async with self._semaphore:
                    self._stats["total_requests"] += 1

                    response = await self._session.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json,
                        data=data,
                    )

                    async with response:
                        if response.status == 429:  # Rate limited
                            retry_after = float(response.headers.get("Retry-After", retry_base_delay))
                            logger.warning(f"Rate limited by {host}, retrying after {retry_after}s")
                            await asyncio.sleep(retry_after)
                            continue  # Retry

                        response.raise_for_status()
                        result = await response.json()

                        # Record success
                        self._stats["successful_requests"] += 1
                        self._record_success(host)
                        return result

            except aiohttp.ClientResponseError as e:
                if e.status == 404:
                    # Not found - don't retry
                    logger.debug(f"Resource not found: {url}")
                    self._stats["failed_requests"] += 1
                    self._record_failure(host)
                    return None
                else:
                    logger.warning(f"HTTP error on attempt {attempt + 1}: {e}")
                    if attempt == max_retries:
                        self._stats["failed_requests"] += 1
                        self._record_failure(host)
                        return None

            except (TimeoutError, aiohttp.ClientError) as e:
                logger.warning(f"Request error on attempt {attempt + 1}: {e}")
                if attempt < max_retries:
                    # Exponential backoff
                    delay = retry_base_delay * (2**attempt)
                    await asyncio.sleep(delay)
                    self._stats["retried_requests"] += 1
                else:
                    self._stats["failed_requests"] += 1
                    self._record_failure(host)
                    return None

        return None

    async def batch_fetch(
        self,
        requests: list[tuple[str, str, dict[str, Any] | None]],
        max_concurrent: int | None = None,
    ) -> list[dict[str, Any] | None]:
        """
        Fetch multiple URLs concurrently with batching.

        Args:
            requests: List of (method, url, kwargs) tuples
            max_concurrent: Override default concurrency limit

        Returns:
            List of responses in same order as requests
        """
        if max_concurrent is None:
            max_concurrent = self._max_concurrent

        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_one(req: tuple[str, str, dict[str, Any] | None]) -> dict[str, Any] | None:
            async with semaphore:
                method, url, kwargs = req
                return await self.fetch(method, url, **(kwargs or {}))

        results = await asyncio.gather(*[fetch_one(req) for req in requests], return_exceptions=True)

        # Handle exceptions
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Batch request {i} failed: {result}")
                final_results.append(None)
            else:
                final_results.append(result)

        return final_results

    def _extract_host(self, url: str) -> str:
        """Extract host from URL for rate limiting and circuit breaker."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.netloc or parsed.path

    def _is_circuit_open(self, host: str) -> bool:
        """Check if circuit breaker is open for host."""
        if not self._enable_circuit_breaker:
            return False

        if host not in self._circuit_state:
            return False

        state = self._circuit_state[host]
        if state.get("open_until", 0) > time.time():
            return True

        # Circuit has cooled down, reset
        if state["failures"] >= self._circuit_breaker_threshold:
            logger.info(f"Circuit breaker cooled down for {host}")
        self._circuit_state[host] = {"failures": 0, "last_failure": None, "open_until": 0}
        return False

    def _record_success(self, host: str) -> None:
        """Record successful request for circuit breaker."""
        if not self._enable_circuit_breaker:
            return

        if host in self._circuit_state:
            # Reset failure count on success
            self._circuit_state[host]["failures"] = 0

    def _record_failure(self, host: str) -> None:
        """Record failed request for circuit breaker."""
        if not self._enable_circuit_breaker:
            return

        if host not in self._circuit_state:
            self._circuit_state[host] = {"failures": 0, "last_failure": None, "open_until": 0}

        state = self._circuit_state[host]
        state["failures"] += 1
        state["last_failure"] = time.time()

        if state["failures"] >= self._circuit_breaker_threshold:
            state["open_until"] = time.time() + self._circuit_breaker_timeout
            logger.warning(
                f"Circuit breaker opened for {host} "
                f"(threshold={self._circuit_breaker_threshold}, "
                f"timeout={self._circuit_breaker_timeout}s)"
            )
            self._stats["circuit_breaker_trips"] += 1

    async def _wait_for_rate_limit(self, host: str) -> None:
        """Apply rate limiting for host."""
        if host not in self._rate_limits:
            return

        min_seconds = self._rate_limits[host]
        last_request = self._last_request_time.get(host, 0)
        now = time.time()
        elapsed = now - last_request

        if elapsed < min_seconds:
            wait_time = min_seconds - elapsed
            logger.debug(f"Rate limiting {host}: waiting {wait_time:.3f}s")
            await asyncio.sleep(wait_time)

        self._last_request_time[host] = time.time()

    def get_stats(self) -> dict[str, Any]:
        """Get pool statistics."""
        success_rate = (
            self._stats["successful_requests"] / self._stats["total_requests"]
            if self._stats["total_requests"] > 0
            else 0
        )

        return {
            **self._stats,
            "success_rate": f"{success_rate:.1%}",
            "pending_requests": self._semaphore._value if self._semaphore else 0,
            "circuit_breaker_state": {
                host: {
                    "failures": state["failures"],
                    "is_open": state.get("open_until", 0) > time.time(),
                }
                for host, state in self._circuit_state.items()
            },
        }

    def reset_circuit_breaker(self, host: str | None = None) -> None:
        """
        Reset circuit breaker for host or all hosts.

        Args:
            host: Specific host to reset, or None for all hosts
        """
        if host:
            if host in self._circuit_state:
                del self._circuit_state[host]
                logger.info(f"Circuit breaker reset for {host}")
        else:
            self._circuit_state.clear()
            logger.info("Circuit breaker reset for all hosts")


# Global singleton for shared use
_global_pool: RESTAPIPool | None = None


def get_global_pool() -> RESTAPIPool:
    """Get or create global REST API pool singleton."""
    global _global_pool
    if _global_pool is None:
        _global_pool = RESTAPIPool()
    return _global_pool


async def initialize_global_pool() -> None:
    """Initialize global REST API pool."""
    pool = get_global_pool()
    await pool.initialize()


async def close_global_pool() -> None:
    """Close global REST API pool."""
    global _global_pool
    if _global_pool:
        await _global_pool.close()
        _global_pool = None
