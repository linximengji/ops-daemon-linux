import time


STATES = ("normal", "degraded", "alerting", "repairing", "recovered", "exhausted")


class CheckStateMachine:
    def __init__(self, name: str, max_attempts: int = 2, cooldown_s: float = 120,
                 degrade_after: int = 2, exhaust_ttl_s: float = 3600,
                 max_exhaustions: int = 3):
        self.name = name
        self.state = "normal"
        self.fail_streak = 0
        self.attempts = 0
        self.last_repair_ts = 0.0
        self.state_since = time.time()
        self.max_attempts = max_attempts
        self.cooldown_s = cooldown_s
        self.degrade_after = degrade_after
        self.exhaust_ttl_s = exhaust_ttl_s
        self.max_exhaustions = max_exhaustions
        self.total_exhaustions = 0
        self._notified_degraded = False



    def record_failure(self) -> str | None:
        self.fail_streak += 1
        now = time.time()

        if self.state == "exhausted":
            if now - self.state_since >= self.exhaust_ttl_s:
                if self.total_exhaustions >= self.max_exhaustions:
                    return None  # permanently exhausted
                self.total_exhaustions += 1
                self._transition("degraded")
                self.fail_streak = 1
                self.attempts = 0
                return None
            return None

        if self.state == "repairing":
            return None

        if self.state == "normal":
            if self.fail_streak >= self.degrade_after:
                self._transition("degraded")
                return self._maybe_alert(now)
            return None

        if self.state == "degraded":
            return self._maybe_alert(now)

        if self.state == "alerting":
            return self._maybe_alert(now)

        if self.state == "recovered":
            self._transition("normal")
            self.fail_streak = 1
            return self.record_failure()

        return None

    def record_success(self):
        self._notified_degraded = False
        self.last_repair_ts = 0
        if self.state in ("normal", "degraded", "alerting"):
            self._transition("normal")
            self.fail_streak = 0
            self.attempts = 0
        elif self.state == "recovered":
            self._transition("normal")
            self.fail_streak = 0
            self.attempts = 0
        elif self.state in ("repairing", "exhausted"):
            pass

    def start_repair(self):
        self._transition("repairing")
        self.attempts += 1
        self.last_repair_ts = time.time()

    def repair_done(self, success: bool) -> str:
        if success:
            self._transition("recovered")
            return "recovered"
        if self.attempts >= self.max_attempts:
            self._transition("exhausted")
            return "exhausted"
        self._transition("degraded")
        return "degraded"

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "fail_streak": self.fail_streak,
            "attempts": self.attempts,
            "state_since": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.state_since)),
            "last_repair_ts": self.last_repair_ts,
        }

    def _maybe_alert(self, now: float) -> str | None:
        if self.state == "degraded":
            if now - self.last_repair_ts >= self.cooldown_s:
                self._transition("alerting")
                self._notified_degraded = False
                return "repair"
            if not self._notified_degraded:
                self._notified_degraded = True
                return "degraded"
            return None
        if self.state == "alerting":
            if now - self.last_repair_ts >= self.cooldown_s:
                return "repair"
            return None
        return None

    def _transition(self, to: str):
        self.state = to
        self.state_since = time.time()
