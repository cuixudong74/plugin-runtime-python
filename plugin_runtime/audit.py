"""Append-only audit log for capability calls and rejections.

Every external capability call -- allowed or rejected -- is recorded.
Rejections are never silently ignored: they are both logged and raised to
the caller.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, List, Optional


@dataclass
class AuditEntry:
    ts: datetime
    caller: str          # plugin id that made the call ("<host>" for host)
    capability: str      # capability name
    kind: str            # "call" | "reject"
    reason: str = ""     # rejection reason (empty for allowed calls)
    provider: Optional[str] = None  # plugin that served the call

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(),
            "caller": self.caller,
            "capability": self.capability,
            "kind": self.kind,
            "reason": self.reason,
            "provider": self.provider,
        }


class AuditLog:
    """Thread-safe bounded audit log.

    ``max_entries`` bounds memory; the oldest entries are dropped when the
    bound is exceeded (the log is a ring buffer, not a source of truth).
    """

    def __init__(self, max_entries: int = 10000):
        self._lock = threading.Lock()
        self._entries: Deque[AuditEntry] = deque(maxlen=max_entries)

    def record(
        self,
        caller: str,
        capability: str,
        kind: str,
        *,
        reason: str = "",
        provider: Optional[str] = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            ts=datetime.now(timezone.utc),
            caller=caller,
            capability=capability,
            kind=kind,
            reason=reason,
            provider=provider,
        )
        with self._lock:
            self._entries.append(entry)
        return entry

    def call(self, caller: str, capability: str, provider: str) -> AuditEntry:
        return self.record(caller, capability, "call", provider=provider)

    def reject(self, caller: str, capability: str, reason: str) -> AuditEntry:
        return self.record(caller, capability, "reject", reason=reason)

    def entries(self, *, caller: Optional[str] = None,
                capability: Optional[str] = None,
                kind: Optional[str] = None,
                limit: int = 100) -> List[AuditEntry]:
        with self._lock:
            items = list(self._entries)
        if caller is not None:
            items = [e for e in items if e.caller == caller]
        if capability is not None:
            items = [e for e in items if e.capability == capability]
        if kind is not None:
            items = [e for e in items if e.kind == kind]
        return items[-limit:]

    def rejections(self, *, caller: Optional[str] = None,
                   capability: Optional[str] = None,
                   limit: int = 100) -> List[AuditEntry]:
        return self.entries(caller=caller, capability=capability,
                            kind="reject", limit=limit)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
