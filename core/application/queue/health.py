"""Application health and circuit-breaker policies for operation dispatch."""
from __future__ import annotations
import time
from dataclasses import dataclass
from core.log import logger

@dataclass
class BotHealth:
    """Per-account health counters used by the scan circuit breaker."""
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    cooldown_until: float = 0.0
    @property
    def is_cooling(self) -> bool:
        return time.monotonic() < self.cooldown_until
    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.cooldown_until = 0.0
        self.total_successes += 1
    def record_failure(self, cooldown_seconds: float = 30.0) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        if self.consecutive_failures >= 3:
            self.cooldown_until = time.monotonic() + cooldown_seconds
            logger.warning(f"[circuit-breaker] bot entering cooldown {cooldown_seconds}s (consecutive={self.consecutive_failures})")

class HealthCircuitBreaker:
    """Owns account health state while keeping dispatcher behavior unchanged."""
    def __init__(self):
        self.bot_health: dict[str, BotHealth] = {}
    def health_for(self, bot_id: str) -> BotHealth:
        return self.bot_health.setdefault(bot_id, BotHealth())
