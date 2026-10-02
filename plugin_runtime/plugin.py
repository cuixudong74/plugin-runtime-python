"""A loaded plugin instance: wraps the manifest, the plugin object and
its lifecycle hooks, and enforces permission checks on external calls."""

from __future__ import annotations

import threading
from typing import Any, Callable

from .errors import (
    PluginRuntimeError,
    PluginStateError,
    QuarantinedError,
)
from .isolation import HealthState, PluginHealth
from .manifest import PluginManifest
from .permissions import PermissionChecker


class PluginContext:
    """Handle given to a plugin to invoke external capabilities.

    Every call is checked against the plugin's declared permissions and
    recorded in the audit log.
    """

    def __init__(self, manifest: PluginManifest, checker: PermissionChecker,
                 host_services: dict[str, Callable]):
        self._manifest = manifest
        self._checker = checker
        self._host_services = host_services
        self._declared = frozenset(manifest.permissions)

    @property
    def plugin_id(self) -> str:
        return self._manifest.plugin_id

    def call(self, capability: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a host capability; undeclared calls raise PermissionDeniedError."""
        self._checker.check(self._manifest.plugin_id, self._declared, capability)
        service = self._host_services.get(capability)
        if service is None:
            raise PluginRuntimeError(
                f"host capability '{capability}' is not provided by this runtime"
            )
        return service(*args, **kwargs)


class PluginInstance:
    """One loaded version of a plugin with refcount-based draining.

    While draining, no new requests are accepted; once the in-flight
    count reaches zero the instance is fully drained and can be unloaded.
    """

    def __init__(self, manifest: PluginManifest, plugin_obj: Any,
                 health: PluginHealth, context: PluginContext):
        self._manifest = manifest
        self._plugin_obj = plugin_obj
        self._health = health
        self._context = context
        self._lock = threading.Lock()
        self._in_flight = 0
        self._draining = False
        self._drained = threading.Event()

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    @property
    def context(self) -> PluginContext:
        return self._context

    @property
    def plugin_obj(self) -> Any:
        return self._plugin_obj

    @property
    def draining(self) -> bool:
        with self._lock:
            return self._draining

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def acquire(self) -> None:
        """Pin this version for an in-flight request."""
        with self._lock:
            if self._draining:
                raise PluginStateError(
                    f"plugin '{self._manifest.plugin_id}' "
                    f"{self._manifest.version} is draining and accepts no new requests"
                )
            self._in_flight += 1

    def release(self) -> None:
        """Release a pin; when draining and the count hits zero, unload."""
        with self._lock:
            if self._in_flight <= 0:
                raise PluginStateError("release without matching acquire")
            self._in_flight -= 1
            if self._draining and self._in_flight == 0:
                self._drained.set()

    def begin_drain(self) -> None:
        """Stop accepting new requests; unload once in-flight ones finish."""
        with self._lock:
            self._draining = True
            if self._in_flight == 0:
                self._drained.set()

    def wait_drained(self, timeout: float | None = None) -> bool:
        """Block until drained; returns False on timeout."""
        return self._drained.wait(timeout)

    def execute(self, capability: str, *args: Any, **kwargs: Any) -> Any:
        """Call into the plugin, recording failures for isolation."""
        if self._health.state is HealthState.QUARANTINED:
            raise QuarantinedError(
                self._manifest.plugin_id,
                self._health.quarantine_reason or "quarantined",
            )
        if capability not in self._manifest.capabilities:
            raise PluginRuntimeError(
                f"plugin '{self._manifest.plugin_id}' does not expose "
                f"capability '{capability}'"
            )
        handler = getattr(self._plugin_obj, capability.replace("-", "_"), None)
        if not callable(handler):
            raise PluginRuntimeError(
                f"plugin '{self._manifest.plugin_id}' has no callable for "
                f"capability '{capability}'"
            )
        try:
            result = handler(*args, **kwargs)
        except Exception as exc:
            self._health.record_failure("execute", f"{type(exc).__name__}: {exc}")
            raise
        self._health.record_success()
        return result
