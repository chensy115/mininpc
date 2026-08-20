from __future__ import annotations

import hashlib


class RandomService:
    """Counter-based deterministic random source whose state is easy to persist."""

    def __init__(self, seed: int, counter: int = 0) -> None:
        self.seed = seed
        self.counter = counter

    def _unit(self) -> float:
        payload = f"{self.seed}:{self.counter}".encode()
        self.counter += 1
        value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
        return value / ((1 << 64) - 1)

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self._unit()

    def randint(self, low: int, high: int) -> int:
        return low + int(self._unit() * (high - low + 1))

