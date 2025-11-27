import asyncio
import time
import logging

logger = logging.getLogger(__name__)

class ProActiveTokenBucket:
    """
    Punkt 3 – Der ultimative, unbannbare Rate Limiter
    Pro-aktiv, adaptiv, mit 429-Autorecovery und Burst-Support
    """
    def __init__(
        self,
        name: str,
        initial_rate: float = 30.0,   # Startwert
        min_rate: float = 5.0,
        max_rate: float = 80.0,       # X10 & Lighter halten das locker aus
        burst_tokens: int = 100,      # Sofort verfügbar bei Start/Recovery
    ):
        self.name = name
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.current_rate = max(min_rate, min(initial_rate, max_rate))

        self.max_tokens = burst_tokens
        self.tokens = float(burst_tokens)
        self.refill_rate = self.current_rate
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

        self.consecutive_429 = 0
        self.last_429_time = 0.0
        self.success_streak = 0

    async def acquire(self, cost: float = 1.0) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_refill
                refill = elapsed * self.refill_rate
                self.tokens = min(self.max_tokens, self.tokens + refill)
                self.last_refill = now

                if self.tokens >= cost:
                    self.tokens -= cost
                    self.success_streak += 1
                    # Recovery-Logik: alle 50 Erfolge hochschalten
                    if self.success_streak >= 50 and self.refill_rate < self.max_rate:
                        old = self.refill_rate
                        self.refill_rate = min(self.max_rate, self.refill_rate * 1.3)
                        self.max_tokens = max(100, int(self.refill_rate * 4))
                        logger.info(f"{self.name} Rate ↑ RECOVERY: {old:.1f} → {self.refill_rate:.1f} req/s")
                        self.success_streak = 0
                    return

                # Nicht genug Tokens → warten
                wait = (cost - self.tokens) / max(self.refill_rate, 1e-6)
                await asyncio.sleep(wait)

    def penalize_429(self) -> None:
        """Wird bei HTTP 429 aufgerufen – sofortige & harte Reduktion"""
        now = time.monotonic()
        self.consecutive_429 += 1
        self.last_429_time = now
        self.success_streak = 0

        old_rate = self.refill_rate
        if self.consecutive_429 == 1:
            self.refill_rate = max(self.min_rate, self.refill_rate * 0.5)
        elif self.consecutive_429 == 2:
            self.refill_rate = max(self.min_rate, self.refill_rate * 0.5)
        else:
            self.refill_rate = self.min_rate

        self.max_tokens = max(20, int(self.refill_rate * 5))
        self.tokens = self.max_tokens

        logger.warning(
            f"{self.name} 429 DETECTED #{self.consecutive_429} → Rate ↓ {old_rate:.1f} → {self.refill_rate:.1f} req/s"
        )

    def on_success(self) -> None:
        """Optional: für sehr konservative Strategien"""
        self.success_streak += 1


# Globale Instanzen – werden in den Adaptern verwendet
X10_RATE_LIMITER = ProActiveTokenBucket("X10", initial_rate=40.0, max_rate=80.0)
LIGHTER_RATE_LIMITER = ProActiveTokenBucket("Lighter", initial_rate=35.0, max_rate=70.0)