"""Failure tracking and circuit-breaker isolation.

Each plugin has a consecutive-failure counter.  Successful invocations reset
it; load or execution errors increment it.  When the counter reaches
``threshold`` the plugin is *isolated*: it stops serving requests and its
state becomes ``ISOLATED``.  Isolation state, the last failure reason and a
short history are queryable, and an operator can recover the plugin manually.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, List, Optional


@dataclass
class FailureRecord:
    ts: datetime
    error: str
    operation: str  # "load" | "invoke" | "enable" | "upgrade"

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(),
            "error": self.error,
            "operation": self.operation,
        }


@dataclass
class IsolationStatus:
    plugin_id: str
    isolated: bool
    consecutive_failures: int
    threshold: int
    last_error: Optional[str]
    last_failure_at: Optional[str]
    history: List[dict]

    def to_dict(self) -> dict:
        return {
            "plugin_id": self.plugin_id,
            "isolated": self.isolated,
            "consecutive_failures": self.consecutive_failures,
            "threshold": self.threshold,
            "last_error": self.last_error,
            "last_failure_at": self.last_failure_at,
            "history": self.history,
        }


class FailureTracker:
    """Per-plugin failure counter with circuit-breaker semantics."""

    def __init__(self, plugin_id: str, threshold: int = 5, history_size: int = 20):
        self.plugin_id = plugin_id
        self.threshold = threshold
        self._lock = threading.Lock()
        self._consecutive = 0
        self._last_error: Optional[str] = None
        self._last_failure_at: Optional[datetime] = None
        self._history: Deque[FailureRecord] = deque(maxlen=history_size)
        self._isolated = False

    def record_failure(self, error: str, operation: str) -> bool:
        """Record a failure.  Returns True if the plugin is now isolated."""
        with self._lock:
            self._consecutive += 1
            self._last_error = error
            self._last_failure_at = datetime.now(timezone.utc)
            self._history.append(FailureRecord(
                ts=self._last_failure_at, error=error, operation=operation))
            if self._consecutive >= self.threshold:
                self._isolated = True
            return self._isolated

    def record_success(self) -> None:
        with self._lock:
            self._consecutive = 0
            # A success does not automatically clear isolation; an operator
            # must recover explicitly.  It does reset the failure counter so
            # the plugin can be re-evaluated.

    def is_isolated(self) -> bool:
        with self._lock:
            return self._isolated

    def reset(self) -> None:
        """Clear isolation and failure history (used by recover())."""
        with self._lock:
            self._consecutive = 0
            self._last_error = None
            self._last_failure_at = None
            self._history.clear()
            self._isolated = False

    def status(self) -> IsolationStatus:
        with self._lock:
            return IsolationStatus(
                plugin_id=self.plugin_id,
                isolated=self._isolated,
                consecutive_failures=self._consecutive,
                threshold=self.threshold,
                last_error=self._last_error,
                last_failure_at=(
                    self._last_failure_at.isoformat()
                    if self._last_failure_at else None
                ),
                history=[r.to_dict() for r in self._history],
            )
