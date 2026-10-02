"""Plugin loading: import an entrypoint and wrap it with runtime state.

A plugin entrypoint is declared in the manifest as ``"module:callable"``
(e.g. ``"my_plugin:create_plugin"``).  The module is loaded from the
directory that contains the manifest (``plugin.json``), so each plugin is
self-contained.  The callable is invoked with no arguments and must return
an object implementing the :class:`PluginInstance` protocol (an async
``handle`` method).

Loading failures raise :class:`PluginLoadError`; a failed load never leaves
a half-registered plugin behind.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import threading
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from .errors import PluginLoadError
from .manifest import Manifest
from .versions import Version


@runtime_checkable
class PluginInstance(Protocol):
    """Protocol that all loaded plugins must implement."""

    async def handle(self, capability: str, payload: dict, ctx: Any) -> dict:
        """Handle a capability call.

        :param capability: the capability name being invoked.
        :param payload: caller-supplied data.
        :param ctx: a :class:`~plugin_runtime.context.PluginContext` for
            calling other plugins' capabilities.
        """
        ...


@dataclass
class LoadedPlugin:
    """A loaded plugin version with its runtime bookkeeping."""

    manifest: Manifest
    instance: Any
    version: Version
    in_flight: int = 0
    draining: bool = False
    _drain_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._drain_event.set()  # no in-flight work initially

    def acquire(self) -> bool:
        """Register an in-flight request.  Returns False if draining."""
        if self.draining:
            return False
        self.in_flight += 1
        self._drain_event.clear()
        return True

    def release(self) -> None:
        self.in_flight -= 1
        if self.in_flight <= 0:
            self.in_flight = 0
            self._drain_event.set()

    def wait_drained(self, timeout: float) -> bool:
        """Block until all in-flight requests finish or timeout.

        Returns True if drained, False if the timeout elapsed.

        .. warning::
            This blocks the calling thread.  Prefer
            :meth:`wait_drained_async` from async code.
        """
        if self.in_flight == 0:
            return True
        return self._drain_event.wait(timeout=timeout)

    async def wait_drained_async(self, timeout: float) -> bool:
        """Async, cancellable drain wait.

        Returns True if drained, False if the timeout elapsed.  Polls the
        in-flight counter so that the wait is cancellable and does not block
        the event loop.
        """
        import asyncio
        if self.in_flight == 0:
            return True
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while self.in_flight > 0:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.02, remaining))
        return True


def _load_module(module_name: str, plugin_dir: str) -> ModuleType:
    """Load a module from the plugin directory without polluting sys.path."""
    # Try <dir>/<module>.py then <dir>/<module>/__init__.py
    candidates = [
        os.path.join(plugin_dir, module_name + ".py"),
        os.path.join(plugin_dir, module_name, "__init__.py"),
    ]
    path = None
    for cand in candidates:
        if os.path.isfile(cand):
            path = cand
            break
    if path is None:
        raise PluginLoadError(
            f"cannot find module {module_name!r} in {plugin_dir!r} "
            f"(looked for {module_name}.py and {module_name}/__init__.py)"
        )
    # Use a unique module name to avoid collisions between plugins.
    unique_name = f"_plugin_runtime_{module_name}_{abs(hash(plugin_dir))}"
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"cannot create import spec for {path!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(unique_name, None)
        raise PluginLoadError(
            f"error while importing module {module_name!r}: {exc}",
            cause=exc,
        ) from exc
    return module


def load_plugin(manifest: Manifest) -> LoadedPlugin:
    """Load the plugin described by ``manifest``.

    Raises :class:`PluginLoadError` on any failure.  The returned
    :class:`LoadedPlugin` is fully initialized and ready to serve.
    """
    entrypoint = manifest.entrypoint
    if ":" not in entrypoint:
        raise PluginLoadError(
            f"entrypoint {entrypoint!r} must be 'module:callable'",
            plugin_id=manifest.id,
        )
    module_name, attr_name = entrypoint.split(":", 1)
    if not module_name or not attr_name:
        raise PluginLoadError(
            f"entrypoint {entrypoint!r} must be 'module:callable'",
            plugin_id=manifest.id,
        )

    plugin_dir = os.path.dirname(os.path.abspath(manifest.source or "."))
    try:
        module = _load_module(module_name, plugin_dir)
    except PluginLoadError:
        raise
    except Exception as exc:
        raise PluginLoadError(
            f"failed to load module {module_name!r}: {exc}",
            plugin_id=manifest.id,
            cause=exc,
        ) from exc

    if not hasattr(module, attr_name):
        raise PluginLoadError(
            f"module {module_name!r} has no attribute {attr_name!r}",
            plugin_id=manifest.id,
        )
    factory = getattr(module, attr_name)
    if not callable(factory):
        raise PluginLoadError(
            f"entrypoint {attr_name!r} is not callable",
            plugin_id=manifest.id,
        )
    try:
        instance = factory()
    except Exception as exc:
        raise PluginLoadError(
            f"plugin factory {entrypoint!r} raised: {exc}",
            plugin_id=manifest.id,
            cause=exc,
        ) from exc

    if not hasattr(instance, "handle") or not inspect.iscoroutinefunction(
        getattr(instance, "handle", None)
    ):
        raise PluginLoadError(
            f"plugin instance must provide an async 'handle' method",
            plugin_id=manifest.id,
        )

    return LoadedPlugin(manifest=manifest, instance=instance,
                        version=manifest.version)
