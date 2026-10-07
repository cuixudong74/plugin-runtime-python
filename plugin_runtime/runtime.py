"""The plugin runtime: install, enable, disable and upgrade plugins
without restarting the host.

Concurrency model: a single async lock serializes the *commit* of every
lifecycle mutation (resolve + load + atomic swap + begin-drain). The
commit is fully synchronous — no awaits between the first and the last
mutation — so a timeout or cancellation can never leave the runtime in
a half-mutated state: either the swap happened (new consistent state)
or it did not (old consistent state).

Draining (waiting for in-flight requests on old versions to finish)
happens *after* the commit, outside the lock. One plugin draining
therefore never blocks lifecycle operations on unrelated plugins, and
the draining wait itself is event-driven: the operation returns as soon
as the last in-flight request finishes, not when the drain timeout
expires. The timeout is only an upper bound for stuck requests.

Callers always see exactly one complete version per plugin: the old
version stops accepting new requests at the commit instant and is
unloaded once its in-flight requests finish; new requests land on the
new version (or get a clear "not enabled" rejection after a disable).
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import time
from dataclasses import dataclass
from typing import Any, Callable

from .errors import (
    PluginRuntimeError,
    PluginStateError,
    ResolutionError,
    ResolutionErrorReason,
)
from .isolation import PluginHealth
from .manifest import PluginManifest
from .permissions import AuditLog, PermissionChecker
from .plugin import PluginContext, PluginInstance
from .resolver import ResolutionPlan, resolve
from .version import Version


class PluginStatus(enum.Enum):
    INSTALLED = "installed"      # manifest stored, not loaded
    ENABLED = "enabled"          # loaded and serving
    DISABLED = "disabled"        # was enabled, now drained and unloaded
    QUARANTINED = "quarantined"  # isolated after repeated failures


@dataclass(frozen=True)
class PluginInfo:
    """External, queryable view of a plugin."""

    plugin_id: str
    version: str | None
    status: PluginStatus
    available_versions: tuple[str, ...]
    health: dict
    pending_requests: int


# Loader: given a manifest, return the plugin object (must expose the
# declared capabilities as callables). Raises on load failure.
PluginLoader = Callable[[PluginManifest], Any]


def _set_if_pending(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(None)


def _default_loader(manifest: PluginManifest) -> Any:
    """Load ``entrypoint`` (``module:attr``); classes are instantiated."""
    if not manifest.entrypoint:
        raise PluginRuntimeError(
            f"plugin '{manifest.plugin_id}' declares no entrypoint"
        )
    module_name, _, attr = manifest.entrypoint.partition(":")
    if not module_name or not attr:
        raise PluginRuntimeError(
            f"invalid entrypoint {manifest.entrypoint!r}; expected 'module:attr'"
        )
    obj = getattr(importlib.import_module(module_name), attr)
    if isinstance(obj, type):
        obj = obj()
    return obj


class PluginRuntime:
    """Coordinates manifests, resolution, lifecycle and isolation."""

    def __init__(
        self,
        loader: PluginLoader | None = None,
        host_services: dict[str, Callable] | None = None,
        failure_threshold: int = 3,
        drain_timeout: float = 30.0,
        audit_log: AuditLog | None = None,
    ):
        self._loader = loader or _default_loader
        self._host_services = dict(host_services or {})
        self._failure_threshold = failure_threshold
        self._drain_timeout = drain_timeout
        self.audit_log = audit_log or AuditLog()
        self._checker = PermissionChecker(self.audit_log)

        self._installed: dict[str, dict[Version, PluginManifest]] = {}
        self._enabled: dict[str, PluginInstance] = {}
        self._health: dict[str, PluginHealth] = {}
        self._disabled: set[str] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _health_of(self, plugin_id: str) -> PluginHealth:
        if plugin_id not in self._health:
            self._health[plugin_id] = PluginHealth(
                plugin_id, failure_threshold=self._failure_threshold
            )
        return self._health[plugin_id]

    def _status_of(self, plugin_id: str) -> PluginStatus:
        health = self._health_of(plugin_id)
        if health.is_quarantined:
            return PluginStatus.QUARANTINED
        if plugin_id in self._enabled:
            return PluginStatus.ENABLED
        if plugin_id in self._disabled:
            return PluginStatus.DISABLED
        return PluginStatus.INSTALLED

    def _info(self, plugin_id: str) -> PluginInfo:
        inst = self._enabled.get(plugin_id)
        versions = tuple(
            str(v) for v in sorted(self._installed.get(plugin_id, {}), reverse=True)
        )
        return PluginInfo(
            plugin_id=plugin_id,
            version=str(inst.manifest.version) if inst else None,
            status=self._status_of(plugin_id),
            available_versions=versions,
            health=self._health_of(plugin_id).status(),
            pending_requests=inst.in_flight if inst else 0,
        )

    def _make_instance(self, manifest: PluginManifest) -> PluginInstance:
        health = self._health_of(manifest.plugin_id)
        try:
            obj = self._loader(manifest)
        except Exception as exc:
            health.record_failure("load", f"{type(exc).__name__}: {exc}")
            raise PluginRuntimeError(
                f"failed to load plugin '{manifest.plugin_id}' "
                f"{manifest.version}: {exc}"
            ) from exc
        context = PluginContext(manifest, self._checker, self._host_services)
        return PluginInstance(manifest, obj, health, context)

    def _candidates_for(self, participants: set[str]) -> dict[str, list[PluginManifest]]:
        return {
            pid: list(self._installed.get(pid, {}).values())
            for pid in sorted(participants)
        }

    def _resolve_for_enable(self, target: str) -> ResolutionPlan:
        """Resolve target + enabled set, auto-including installed deps."""
        participants = set(self._enabled) | {target}
        enabled_map = {
            pid: inst.manifest for pid, inst in self._enabled.items()
        }
        for _ in range(len(self._installed) + 2):
            try:
                return resolve(enabled_map, self._candidates_for(participants))
            except ResolutionError as exc:
                missing = exc.details.get("missing")
                if (
                    exc.reason is ResolutionErrorReason.UNKNOWN_DEPENDENCY
                    and missing in self._installed
                    and missing not in participants
                ):
                    participants.add(missing)
                    continue
                raise
        raise PluginRuntimeError("dependency closure did not converge")

    def _apply_plan(self, plan: ResolutionPlan, remove: set[str]) -> list[PluginInstance]:
        """Swap instances to match the plan. Returns old instances to drain.

        This method is fully synchronous: no cancellation point exists
        between the first and the last mutation. Replaced/removed
        instances stop accepting new requests at the commit instant
        (``begin_drain``), so the old version never serves alongside the
        new one; it only finishes requests already in flight.
        """
        old_instances: list[PluginInstance] = []
        new_instances: dict[str, PluginInstance] = {}
        for pid in plan.order:
            desired = plan.versions[pid]
            current = self._enabled.get(pid)
            if current is not None and current.manifest.version == desired:
                continue  # already serving the resolved version
            manifest = self._installed[pid][desired]
            new_instances[pid] = self._make_instance(manifest)
        # Commit point: swap everything at once.
        for pid in remove:
            old = self._enabled.pop(pid, None)
            if old is not None:
                old_instances.append(old)
            self._disabled.add(pid)
        for pid, inst in new_instances.items():
            old = self._enabled.get(pid)
            if old is not None:
                old_instances.append(old)
            self._enabled[pid] = inst
            self._disabled.discard(pid)
        for inst in old_instances:
            inst.begin_drain()
        return old_instances

    async def _wait_drained(self, old_instances: list[PluginInstance], deadline: float) -> None:
        """Wait for old versions to drain, outside the mutation lock.

        The wait is event-driven: it returns as soon as the last
        in-flight request finishes, not when the deadline arrives, and
        it is cancellable without leaving blocked waiter threads behind
        (no ``asyncio.to_thread`` on a threading.Event). On timeout the
        remaining instances are dropped (the enabled set is already
        consistent), so the runtime never lingers in an intermediate
        state.
        """
        loop = asyncio.get_running_loop()
        for inst in old_instances:
            if inst.drained:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            fut: asyncio.Future = loop.create_future()

            def _resolve(f: asyncio.Future = fut) -> None:
                try:
                    loop.call_soon_threadsafe(_set_if_pending, f)
                except RuntimeError:  # loop already closed
                    pass

            inst.on_drained(_resolve)
            try:
                await asyncio.wait_for(fut, remaining)
            except TimeoutError:
                pass  # dropped; the enabled set is already consistent
        # Anything not drained by now is dropped; the enabled set is
        # already consistent, so this is safe.

    async def _serialized(self, timeout: float | None, body) -> Any:
        """Run ``body`` with an overall timeout.

        ``body`` receives the operation deadline; it commits state under
        the lock and drains afterwards, outside the lock.
        """
        effective = timeout if timeout is not None else self._drain_timeout
        try:
            async with asyncio.timeout(effective):
                return await body(time.monotonic() + effective)
        except TimeoutError as exc:
            raise PluginRuntimeError(
                "operation timed out; runtime left in a consistent state"
            ) from exc

    # ------------------------------------------------------------------
    # installation
    # ------------------------------------------------------------------
    async def install(self, manifest_json: str | bytes) -> PluginManifest:
        """Validate and store a manifest. Invalid manifests are rejected
        wholesale; nothing is partially loaded. Re-installing the same
        version is a no-op."""
        manifest = PluginManifest.from_json(manifest_json)  # raises before any mutation
        async with self._lock:
            self._installed.setdefault(manifest.plugin_id, {})[manifest.version] = manifest
        return manifest

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def enable(self, plugin_id: str, timeout: float | None = None) -> PluginInfo:
        """Resolve dependencies and activate a consistent plugin set."""
        async def body(deadline: float) -> PluginInfo:
            async with self._lock:
                if plugin_id not in self._installed:
                    raise PluginStateError(f"plugin '{plugin_id}' is not installed")
                current = self._enabled.get(plugin_id)
                if current is not None:
                    return self._info(plugin_id)  # idempotent
                plan = self._resolve_for_enable(plugin_id)
                old = self._apply_plan(plan, remove=set())
            await self._wait_drained(old, deadline)
            return self._info(plugin_id)

        return await self._serialized(timeout, body)

    async def disable(self, plugin_id: str, timeout: float | None = None) -> PluginInfo:
        """Drain and deactivate a plugin together with its dependents."""
        async def body(deadline: float) -> PluginInfo:
            async with self._lock:
                if plugin_id not in self._installed:
                    raise PluginStateError(f"plugin '{plugin_id}' is not installed")
                if plugin_id not in self._enabled:
                    self._disabled.add(plugin_id)
                    return self._info(plugin_id)  # idempotent
                # Everything depending (transitively) on the target must go too.
                removed = {plugin_id}
                changed = True
                while changed:
                    changed = False
                    for pid, inst in self._enabled.items():
                        if pid in removed:
                            continue
                        if any(d.plugin_id in removed for d in inst.manifest.dependencies):
                            removed.add(pid)
                            changed = True
                remaining = {
                    pid: inst.manifest
                    for pid, inst in self._enabled.items()
                    if pid not in removed
                }
                plan = resolve(remaining, self._candidates_for(set(remaining)))
                old = self._apply_plan(plan, remove=removed)
            await self._wait_drained(old, deadline)
            return self._info(plugin_id)

        return await self._serialized(timeout, body)

    async def upgrade(self, plugin_id: str, timeout: float | None = None) -> PluginInfo:
        """Switch to the newest resolvable version; the old version is
        drained (in-flight requests finish) before it is unloaded, so
        callers always see exactly one complete version."""
        async def body(deadline: float) -> PluginInfo:
            async with self._lock:
                if plugin_id not in self._installed:
                    raise PluginStateError(f"plugin '{plugin_id}' is not installed")
                # Re-resolve with the target's pool ordered newest-first by
                # excluding it from the "keep current" map.
                enabled_map = {
                    pid: inst.manifest
                    for pid, inst in self._enabled.items()
                    if pid != plugin_id
                }
                participants = set(enabled_map) | {plugin_id}
                plan = None
                for _ in range(len(self._installed) + 2):
                    try:
                        plan = resolve(enabled_map, self._candidates_for(participants))
                        break
                    except ResolutionError as exc:
                        missing = exc.details.get("missing")
                        if (
                            exc.reason is ResolutionErrorReason.UNKNOWN_DEPENDENCY
                            and missing in self._installed
                            and missing not in participants
                        ):
                            participants.add(missing)
                            continue
                        raise
                if plan is None:
                    raise PluginRuntimeError("dependency closure did not converge")
                old = self._apply_plan(plan, remove=set())
            await self._wait_drained(old, deadline)
            return self._info(plugin_id)

        return await self._serialized(timeout, body)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def list_plugins(self) -> list[PluginInfo]:
        return [self._info(pid) for pid in sorted(self._installed)]

    def get_plugin(self, plugin_id: str) -> PluginInfo:
        if plugin_id not in self._installed:
            raise PluginStateError(f"plugin '{plugin_id}' is not installed")
        return self._info(plugin_id)

    def audit_records(self, plugin_id: str | None = None) -> list:
        if plugin_id is None:
            return self.audit_log.all()
        return self.audit_log.for_plugin(plugin_id)

    def recover(self, plugin_id: str) -> PluginInfo:
        """Lift quarantine after the operator has fixed the plugin."""
        if plugin_id not in self._installed:
            raise PluginStateError(f"plugin '{plugin_id}' is not installed")
        self._health_of(plugin_id).reset()
        return self._info(plugin_id)

    # ------------------------------------------------------------------
    # invocation
    # ------------------------------------------------------------------
    async def invoke(self, plugin_id: str, capability: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a capability on the currently enabled version.

        The instance is pinned for the duration of the call, so an
        upgrade happening concurrently drains rather than interrupts it.
        A call that arrives while its target version is being swapped out
        retries briefly and lands on the new version; if the plugin is
        disabled (or stays mid-swap for an unreasonably long time) the
        call is rejected with a clear, distinguishable PluginStateError.
        """
        attempts = 0
        while True:
            inst = self._enabled.get(plugin_id)
            if inst is None:
                raise PluginStateError(f"plugin '{plugin_id}' is not enabled")
            try:
                inst.acquire()
            except PluginStateError:
                # The pinned instance was swapped out and is draining;
                # retry to land on the version that replaced it.
                attempts += 1
                if attempts > 100:
                    raise PluginStateError(
                        f"plugin '{plugin_id}' is being reconfigured; "
                        "retry the call"
                    )
                await asyncio.sleep(0.01)
                continue
            try:
                return inst.execute(capability, *args, **kwargs)
            finally:
                inst.release()
