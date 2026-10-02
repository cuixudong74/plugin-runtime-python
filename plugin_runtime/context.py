"""Runtime context passed to plugin ``handle`` methods.

Plugins use :meth:`PluginContext.call` to invoke capabilities provided by
other plugins.  Every call is checked against the caller's declared
``requires`` list and recorded in the audit log.  Undeclared calls are
rejected with :class:`~plugin_runtime.errors.UndeclaredCapabilityError`
(which propagates to the caller) and are never silently ignored.
"""

from __future__ import annotations

from typing import Any


class PluginContext:
    """Per-request context bound to the calling plugin."""

    def __init__(self, runtime: Any, caller_id: str):
        self._runtime = runtime
        self._caller_id = caller_id

    @property
    def caller_id(self) -> str:
        return self._caller_id

    async def call(self, capability: str, payload: dict) -> dict:
        """Invoke a capability provided by another (or the same) plugin.

        :raises UndeclaredCapabilityError: the caller did not declare this
            capability in its manifest's ``requires`` list.
        :raises IsolationError: the provider is isolated.
        :raises PluginNotFoundError: no enabled plugin provides the
            capability.
        """
        return await self._runtime.invoke_capability(
            self._caller_id, capability, payload
        )
