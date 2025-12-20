# Compatibility shim - This file has been moved to infrastructure/api/
# Import from new location for better organization
from src.infrastructure.api.rate_limiter import (
    TokenBucketRateLimiter,
    RateLimiterConfig,
    Exchange,
    X10_RATE_LIMITER,
    LIGHTER_RATE_LIMITER,
    get_rate_limiter,
    rate_limited,
    with_rate_limit,
    shutdown_all_rate_limiters,
    are_rate_limiters_shutdown,
)

# Backward compatibility aliases
RateLimiter = TokenBucketRateLimiter

__all__ = [
    'TokenBucketRateLimiter',
    'RateLimiter',  # Backward compatibility alias
    'RateLimiterConfig',
    'Exchange',
    'X10_RATE_LIMITER',
    'LIGHTER_RATE_LIMITER',
    'get_rate_limiter',
    'rate_limited',
    'with_rate_limit',
    'shutdown_all_rate_limiters',
    'are_rate_limiters_shutdown',
]
