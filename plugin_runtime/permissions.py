"""Permission checks: a plugin must declare every external capability
before invoking it. Undeclared calls are denied and auditable."""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field

from .errors import PermissionDeniedError


class AuditOutcome(enum.Enum):
    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass(frozen=True)
class AuditRecord:
    timestamp: float
    plugin_id: str
    capability: str
    outcome: AuditOutcome


@dataclass
class AuditLog:
    """Thread-safe in-memory audit log of permission checks."""

    records: list[AuditRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            self.records.append(record)

    def for_plugin(self, plugin_id: str) -> list[AuditRecord]:
        with self._lock:
            return [r for r in self.records if r.plugin_id == plugin_id]

    def all(self) -> list[AuditRecord]:
        with self._lock:
            return list(self.records)


class PermissionChecker:
    """Checks plugin calls against declared permissions and audits them."""

    def __init__(self, audit_log: AuditLog | None = None):
        self.audit_log = audit_log or AuditLog()

    def check(self, plugin_id: str, declared: frozenset[str], capability: str) -> None:
        """Allow the call or raise PermissionDeniedError; always audited.

        A denial is never silent: it is recorded in the audit log and
        surfaced to the caller as an exception.
        """
        allowed = capability in declared
        self.audit_log.append(AuditRecord(
            timestamp=time.time(),
            plugin_id=plugin_id,
            capability=capability,
            outcome=AuditOutcome.ALLOWED if allowed else AuditOutcome.DENIED,
        ))
        if not allowed:
            raise PermissionDeniedError(plugin_id, capability)
