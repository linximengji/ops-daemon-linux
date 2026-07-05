"""RepairCoordinator — state tracking for check-repair lifecycle.

Encapsulates 6 dicts that were previously bare globals in main.py,
eliminating the risk of inconsistent state when multiple callbacks
modify repair state independently.
"""
import time


class RepairCoordinator:
    """Tracks per-check repair state — cooldown, attempts, exhaustion, backoff tiers."""

    def __init__(self, max_attempts: int = 2, cooldown_s: int = 120,
                 exhaust_timeout: int = 3600):
        self.max_attempts = max_attempts
        self.cooldown_s = cooldown_s
        self.exhaust_timeout = exhaust_timeout
        self._attempts: dict[str, int] = {}
        self._cooldown: dict[str, float] = {}
        self._notified: set[str] = set()
        self._exhaust_at: dict[str, float] = {}
        self._backoff_tiers: dict[str, list[int]] = {}
        self._backoff_idx: dict[str, int] = {}

    def on_success(self, name: str):
        """Check passes — clear all repair state for this check."""
        self._attempts.pop(name, None)
        self._cooldown.pop(name, None)
        self._notified.discard(name)
        self._exhaust_at.pop(name, None)
        self._backoff_tiers.pop(name, None)
        self._backoff_idx.pop(name, None)

    def decide(self, name: str, now: float) -> str:
        """Return what the caller should do: 'repair', 'skip', or 'exhausted'.

        'repair' — proceed with repair.
        'skip' — cooldown active or exhaust period not yet elapsed.
        'exhausted' — max attempts reached without recovery this cycle.
        """
        # Exhaust timeout: auto-reset if enough time elapsed
        exhaust_start = self._exhaust_at.get(name)
        if exhaust_start is not None:
            tier_timeout = self.exhaust_timeout
            tiers = self._backoff_tiers.get(name)
            if tiers:
                idx = self._backoff_idx.get(name, 0)
                tier_timeout = tiers[min(idx, len(tiers) - 1)]
            if now - exhaust_start >= tier_timeout:
                self._attempts.pop(name, None)
                self._notified.discard(name)
                self._exhaust_at.pop(name, None)
                if tiers and name in self._backoff_idx:
                    self._backoff_idx[name] = min(
                        self._backoff_idx.get(name, 0) + 1, len(tiers) - 1)
                return 'repair'
            return 'skip'

        # Cooldown check
        last = self._cooldown.get(name, 0.0)
        if now - last < self.cooldown_s:
            return 'skip'

        # Exhausted?
        if self._attempts.get(name, 0) >= self.max_attempts:
            return 'exhausted'

        return 'repair'

    def get_attempts(self, name: str) -> int:
        """Number of repair attempts recorded so far."""
        return self._attempts.get(name, 0)

    def record_attempt(self, name: str, now: float) -> int:
        """Mark one repair attempt, update cooldown. Returns total attempts."""
        self._attempts[name] = self._attempts.get(name, 0) + 1
        self._cooldown[name] = now
        return self._attempts[name]

    def mark_exhausted(self, name: str, now: float):
        """Force-enter exhausted state (used by claudetalk rapid-crash handler)."""
        self._exhaust_at[name] = now
        self._notified.add(name)
        self._attempts[name] = self.max_attempts

    def is_exhausted(self, name: str) -> bool:
        return name in self._exhaust_at

    def was_notified(self, name: str) -> bool:
        return name in self._notified

    def set_backoff_tier(self, name: str, tiers: list[int], idx: int):
        self._backoff_tiers[name] = tiers
        self._backoff_idx[name] = idx

    def get_last_cooldown(self, name: str) -> float:
        return self._cooldown.get(name, 0.0)

    def get_current_tier_timeout(self, name: str) -> int:
        """Get the current tier timeout for an exhausted check, or exhaust_timeout if no tiers."""
        tiers = self._backoff_tiers.get(name)
        if tiers:
            idx = self._backoff_idx.get(name, 0)
            return tiers[min(idx, len(tiers) - 1)]
        return self.exhaust_timeout
