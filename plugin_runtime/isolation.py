"""Failure isolation: track per-plugin failures and quarantine plugins
that fail repeatedly, so one broken plugin cannot affect others."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field


class HealthState(enum.Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"  # some failures, below threshold
    QUARANTINED = "quarantined"  # isolated after repeated failures


@dataclass(frozen=True)
class FailureRecord:
    timestamp: float
    phase: str  # "load" | "execute"
    error: str


@dataclass
class PluginHealth:
    """Health tracking for one plugin.

    Consecutive failures move the plugin HEALTHY -> DEGRADED ->
    QUARANTINED. A successful execution resets the consecutive counter.
    Quarantine is lifted only via ``reset()`` (manual recovery).
    """

    plugin_id: str
    failure_threshold: int = 3
    state: HealthState = HealthState.HEALTHY
    failures: list[FailureRecord] = field(default_factory=list)
    quarantine_reason: str | None = None
    _consecutive: int = 0

    def record_failure(self, phase: str, error: str) -> None:
        """Record a failure; quarantine when the threshold is reached."""
        self.failures.append(FailureRecord(time.time(), phase, error))
        self._consecutive += 1
        if self._consecutive >= self.failure_threshold:
            self.state = HealthState.QUARANTINED
            self.quarantine_reason = (
                f"{self._consecutive} consecutive failures; "
                f"last: {phase} failed: {error}"
            )
        elif self.state is HealthState.HEALTHY:
            self.state = HealthState.DEGRADED

    def record_success(self) -> None:
        """A successful call clears the consecutive-failure counter."""
        self._consecutive = 0
        if self.state is HealthState.DEGRADED:
            self.state = HealthState.HEALTHY

    def reset(self) -> None:
        """Clear failures and lift quarantine (manual recovery)."""
        self.failures.clear()
        self._consecutive = 0
        self.state = HealthState.HEALTHY
        self.quarantine_reason = None

    def status(self) -> dict:
        """Queryable status: state, failures, reason, recovery hint."""
        return {
            "plugin_id": self.plugin_id,
            "state": self.state.value,
            "consecutive_failures": self._consecutive,
            "failure_threshold": self.failure_threshold,
            "quarantine_reason": self.quarantine_reason,
            "recovery": (
                "fix the plugin, then call recover(plugin_id) to lift quarantine"
                if self.state is HealthState.QUARANTINED
                else None
            ),
            "recent_failures": [
                {"timestamp": f.timestamp, "phase": f.phase, "error": f.error}
                for f in self.failures[-10:]
            ],
        }
