"""The plugin runtime facade.

:class:`PluginRuntime` manages the full lifecycle of hot-pluggable plugins:

* **install** -- validate and store a manifest (no loading yet);
* **enable** -- resolve a consistent version set, load, and atomically serve;
* **disable** -- drain in-flight requests and stop serving;
* **upgrade** -- add a new version, resolve, load, swap atomically, drain old;
* **uninstall** -- remove all versions of a disabled plugin;
* **invoke** -- route a request to the active version with failure tracking;
* **recover** -- clear isolation and re-enable.

Concurrency: all lifecycle mutations are serialized by a single
:class:`asyncio.Lock`.  The active-version pointer is swapped atomically, so
every request is served by exactly one complete version -- never a mix.
Failed resolution or loading rejects the whole operation before any state
changes.  Drain timeout rolls the swap back.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .audit import AuditLog
from .context import PluginContext
from .errors import (
    CapabilityConflictError,
    DrainTimeoutError,
    IsolationError,
    LifecycleError,
    OperationCancelledError,
    PluginLoadError,
    PluginNotFoundError,
    PluginStateError,
    ResolutionError,
    UndeclaredCapabilityError,
)
from .isolation import FailureTracker, IsolationStatus
from .loader import LoadedPlugin, load_plugin
from .manifest import Manifest, load_manifest
from .resolver import AvailablePlugin, Resolution, resolve
from .versions import Range, RangeSet, Version

logger = logging.getLogger(__name__)


class PluginState(str, enum.Enum):
    INSTALLED = "installed"   # installed but never enabled
    ENABLED = "enabled"       # active and serving
    DISABLED = "disabled"     # explicitly disabled
    ISOLATED = "isolated"     # failure threshold reached; not serving


@dataclass
class PluginRecord:
    id: str
    manifests: Dict[Version, Manifest] = field(default_factory=dict)
    sources: Dict[Version, str] = field(default_factory=dict)
    state: PluginState = PluginState.INSTALLED
    active_version: Optional[Version] = None

    def latest_version(self) -> Optional[Version]:
        if not self.manifests:
            return None
        return max(self.manifests.keys())

    def manifest_for(self, version: Version) -> Optional[Manifest]:
        return self.manifests.get(version)


class PluginRuntime:
    """Hot-pluggable plugin runtime."""

    def __init__(
        self,
        *,
        drain_timeout: float = 30.0,
        failure_threshold: int = 5,
        audit_max_entries: int = 10000,
    ):
        self._drain_timeout = drain_timeout
        self._failure_threshold = failure_threshold
        self._plugins: Dict[str, PluginRecord] = {}
        self._active: Dict[str, LoadedPlugin] = {}
        self._trackers: Dict[str, FailureTracker] = {}
        self._audit = AuditLog(max_entries=audit_max_entries)
        self._lock = asyncio.Lock()
        # In-flight operation dedup: key -> future.
        self._inflight: Dict[str, "asyncio.Future"] = {}

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_plugins(self) -> List[dict]:
        return [self._plugin_view(pid) for pid in sorted(self._plugins)]

    def get_plugin(self, plugin_id: str) -> dict:
        record = self._require_record(plugin_id)
        return self._plugin_view(plugin_id)

    def _plugin_view(self, plugin_id: str) -> dict:
        record = self._plugins[plugin_id]
        active = self._active.get(plugin_id)
        tracker = self._trackers.get(plugin_id)
        return {
            "id": plugin_id,
            "state": record.state.value,
            "versions": [str(v) for v in sorted(record.manifests.keys())],
            "active_version": (
                str(active.version) if active else None
            ),
            "capabilities": (
                active.manifest.capabilities if active else []
            ),
            "requires": active.manifest.requires if active else [],
            "failure": tracker.status().to_dict() if tracker else None,
        }

    def _require_record(self, plugin_id: str) -> PluginRecord:
        record = self._plugins.get(plugin_id)
        if record is None:
            raise PluginNotFoundError(f"plugin {plugin_id!r} is not installed")
        return record

    def _tracker(self, plugin_id: str) -> FailureTracker:
        if plugin_id not in self._trackers:
            self._trackers[plugin_id] = FailureTracker(
                plugin_id, threshold=self._failure_threshold
            )
        return self._trackers[plugin_id]

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    async def install(self, manifest: Manifest, *, source: Optional[str] = None) -> dict:
        """Install a validated manifest.

        Installing the same id+version is a no-op (idempotent).  Installing a
        new version adds it to the plugin's version set.  No resolution or
        loading happens until :meth:`enable` is called.
        """
        async with self._lock:
            return self._install_locked(manifest, source=source)

    def _install_locked(self, manifest: Manifest, *, source: Optional[str] = None) -> dict:
        record = self._plugins.get(manifest.id)
        if record is None:
            record = PluginRecord(id=manifest.id)
            self._plugins[manifest.id] = record
        if manifest.version in record.manifests:
            # Idempotent: already installed this exact version.
            return self._plugin_view(manifest.id)
        record.manifests[manifest.version] = manifest
        record.sources[manifest.version] = source or manifest.source or ""
        self._tracker(manifest.id)  # ensure a tracker exists
        return self._plugin_view(manifest.id)

    async def install_from_path(self, path: str) -> dict:
        """Load a manifest from ``path`` and install it."""
        manifest = load_manifest(path)
        return await self.install(manifest, source=manifest.source)

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def _build_available(self) -> Dict[str, AvailablePlugin]:
        available: Dict[str, AvailablePlugin] = {}
        for pid, record in self._plugins.items():
            versions = sorted(record.manifests.keys())
            available[pid] = AvailablePlugin(
                id=pid,
                versions=versions,
                manifests=dict(record.manifests),
            )
        return available

    def _enabled_ids(self) -> Set[str]:
        return {
            pid for pid, rec in self._plugins.items()
            if rec.state == PluginState.ENABLED
        }

    def _resolve_targets(self, extra_enable: Optional[str] = None,
                         extra_disable: Optional[str] = None,
                         version_overrides: Optional[Dict[str, Version]] = None) -> Resolution:
        """Resolve the target enabled set.

        :param extra_enable: plugin id to add to the enabled set.
        :param extra_disable: plugin id to remove from the enabled set
            (also excluded from the available set, so dependents cannot
            pull it back in as a transitive dependency).
        :param version_overrides: pin a plugin to a specific version
            (used by upgrade to force the new version to be selected).
        """
        version_overrides = version_overrides or {}
        targets: Dict[str, RangeSet] = {}
        for pid in self._enabled_ids():
            if pid == extra_disable:
                continue
            if pid in version_overrides:
                targets[pid] = RangeSet((Range("==", version_overrides[pid]),))
            else:
                targets[pid] = RangeSet()  # any version of enabled plugins
        if extra_enable is not None and extra_enable not in targets:
            if extra_enable in version_overrides:
                targets[extra_enable] = RangeSet((Range("==", version_overrides[extra_enable]),))
            else:
                targets[extra_enable] = RangeSet()
        available = self._build_available()
        if extra_disable is not None and extra_disable in available:
            del available[extra_disable]
        resolution = resolve(available, targets)
        self._check_capability_conflicts(resolution)
        return resolution

    def _check_capability_conflicts(self, resolution: Resolution) -> None:
        """Reject if two selected plugins provide the same capability."""
        providers: Dict[str, List[str]] = {}
        for pid, version in resolution.items():
            record = self._plugins.get(pid)
            if record is None:
                continue
            manifest = record.manifest_for(version)
            if manifest is None:
                continue
            for cap in manifest.capabilities:
                providers.setdefault(cap, []).append(pid)
        for cap, pids in providers.items():
            if len(pids) > 1:
                pids.sort()
                raise CapabilityConflictError(
                    f"capability {cap!r} is provided by multiple plugins: "
                    f"{pids}",
                    capability=cap,
                    providers=pids,
                )

    # ------------------------------------------------------------------
    # Enable
    # ------------------------------------------------------------------

    async def enable(self, plugin_id: str) -> dict:
        """Enable an installed plugin (idempotent).

        Resolves a consistent version set over all enabled plugins plus this
        one, loads the selected versions, and atomically starts serving.
        """
        async with self._lock:
            record = self._require_record(plugin_id)
            if record.state == PluginState.ENABLED:
                return self._plugin_view(plugin_id)
            if record.state == PluginState.ISOLATED:
                raise IsolationError(
                    f"plugin {plugin_id!r} is isolated; recover it before enabling",
                    plugin_id=plugin_id,
                    reason=self._tracker(plugin_id).status().last_error,
                )
            # Resolve before touching anything.
            resolution = self._resolve_targets(extra_enable=plugin_id)
            await self._commit_resolution(resolution, reason="enable")
            return self._plugin_view(plugin_id)

    # ------------------------------------------------------------------
    # Disable
    # ------------------------------------------------------------------

    async def disable(self, plugin_id: str) -> dict:
        """Disable an enabled plugin, draining in-flight requests first."""
        async with self._lock:
            record = self._require_record(plugin_id)
            if record.state != PluginState.ENABLED:
                raise PluginStateError(
                    f"plugin {plugin_id!r} is {record.state.value}; cannot disable",
                    current_state=record.state.value,
                    required_state=PluginState.ENABLED.value,
                )
            # Resolve the set without this plugin; if another enabled plugin
            # requires it, resolution fails.
            resolution = self._resolve_targets(extra_disable=plugin_id)
            await self._commit_resolution(resolution, reason="disable",
                                          disabled_id=plugin_id)
            return self._plugin_view(plugin_id)

    # ------------------------------------------------------------------
    # Upgrade
    # ------------------------------------------------------------------

    async def upgrade(self, plugin_id: str, manifest: Manifest,
                      *, source: Optional[str] = None) -> dict:
        """Upgrade a plugin to a new manifest version.

        The new version is installed, resolved, loaded, and swapped in
        atomically.  The old version is drained before being unloaded.
        """
        async with self._lock:
            record = self._require_record(plugin_id)
            if manifest.id != plugin_id:
                raise LifecycleError(
                    f"manifest id {manifest.id!r} does not match plugin {plugin_id!r}"
                )
            if record.state == PluginState.ISOLATED:
                raise IsolationError(
                    f"plugin {plugin_id!r} is isolated; recover it before upgrading",
                    plugin_id=plugin_id,
                )
            # Install the new version (validated already).
            self._install_locked(manifest, source=source)
            # If it was enabled, resolve over the enabled set, pinning the
            # upgraded plugin to the new version.  If the new version cannot
            # be part of any consistent set, resolution fails and the
            # upgrade is rejected (no state change).
            if record.state == PluginState.ENABLED:
                resolution = self._resolve_targets(
                    version_overrides={plugin_id: manifest.version}
                )
                await self._commit_resolution(resolution, reason="upgrade")
                return self._plugin_view(plugin_id)
            # Not enabled: just install the new version.
            return self._plugin_view(plugin_id)

    async def upgrade_from_path(self, plugin_id: str, path: str) -> dict:
        manifest = load_manifest(path)
        return await self.upgrade(plugin_id, manifest, source=manifest.source)

    # ------------------------------------------------------------------
    # Uninstall
    # ------------------------------------------------------------------

    async def uninstall(self, plugin_id: str) -> dict:
        """Remove all versions of a disabled plugin."""
        async with self._lock:
            record = self._require_record(plugin_id)
            if record.state == PluginState.ENABLED:
                raise PluginStateError(
                    f"plugin {plugin_id!r} is enabled; disable before uninstalling",
                    current_state=record.state.value,
                    required_state=PluginState.DISABLED.value,
                )
            if plugin_id in self._active:
                raise PluginStateError(
                    f"plugin {plugin_id!r} still has an active version; disable first",
                    current_state=record.state.value,
                    required_state=PluginState.DISABLED.value,
                )
            del self._plugins[plugin_id]
            self._trackers.pop(plugin_id, None)
            return {"uninstalled": plugin_id}

    # ------------------------------------------------------------------
    # Commit: the atomic swap + drain
    # ------------------------------------------------------------------

    async def _commit_resolution(
        self,
        resolution: Resolution,
        *,
        reason: str,
        disabled_id: Optional[str] = None,
    ) -> dict:
        """Load selected versions and atomically swap the active set.

        On entry the lock is held.  On exit the lock is held (released by
        the caller's ``async with``).
        """
        # 1. Load every selected version that is not already active at the
        #    same version.  Loading happens *before* the swap, so a load
        #    failure leaves the current serving set untouched.
        new_active: Dict[str, LoadedPlugin] = {}
        loaded_by_us: List[LoadedPlugin] = []
        try:
            for pid, version in resolution.items():
                existing = self._active.get(pid)
                if existing is not None and existing.version == version:
                    new_active[pid] = existing
                    continue
                record = self._require_record(pid)
                manifest = record.manifest_for(version)
                if manifest is None:
                    raise PluginLoadError(
                        f"manifest for {pid}@{version} not found",
                        plugin_id=pid,
                    )
                loaded = load_plugin(manifest)  # may raise PluginLoadError
                new_active[pid] = loaded
                loaded_by_us.append(loaded)
        except PluginLoadError as exc:
            # Discard everything we loaded; nothing was swapped.
            for lp in loaded_by_us:
                lp.draining = True
            # Record the load failure toward isolation (load errors count
            # just like execution errors).
            failed_id = exc.plugin_id
            if failed_id is not None and failed_id in self._plugins:
                tracker = self._tracker(failed_id)
                isolated = tracker.record_failure(
                    f"load error: {exc}", operation="load"
                )
                if isolated:
                    self._plugins[failed_id].state = PluginState.ISOLATED
                    self._plugins[failed_id].active_version = None
            raise

        # 2. Snapshot for rollback.
        old_active = dict(self._active)
        old_states = {pid: rec.state for pid, rec in self._plugins.items()}

        # 3. Atomic swap.
        self._active = new_active
        for pid, rec in self._plugins.items():
            if pid in new_active:
                rec.state = PluginState.ENABLED
                rec.active_version = new_active[pid].version
            else:
                rec.state = PluginState.DISABLED
                rec.active_version = None
        if disabled_id is not None and disabled_id in self._plugins:
            # Ensure the explicitly-disabled plugin is disabled.
            self._plugins[disabled_id].state = PluginState.DISABLED
            self._plugins[disabled_id].active_version = None

        # 4. Drain displaced old versions.
        displaced = [
            old for pid, old in old_active.items()
            if pid not in new_active or new_active[pid] is not old
        ]
        for old in displaced:
            old.draining = True

        def _rollback() -> None:
            self._active = old_active
            for pid, state in old_states.items():
                if pid in self._plugins:
                    self._plugins[pid].state = state
                    self._plugins[pid].active_version = (
                        old_active[pid].version if pid in old_active else None
                    )
            for old in displaced:
                old.draining = False
            for lp in loaded_by_us:
                lp.draining = True

        drain_error: Optional[DrainTimeoutError] = None
        try:
            for old in displaced:
                if not await old.wait_drained_async(timeout=self._drain_timeout):
                    drain_error = DrainTimeoutError(
                        f"drain timeout after {self._drain_timeout}s for "
                        f"{old.manifest.id}@{old.version} with {old.in_flight} "
                        f"in-flight request(s)",
                        plugin_id=old.manifest.id,
                        in_flight=old.in_flight,
                    )
                    break
        except asyncio.CancelledError:
            # Cancellation during drain: roll back the swap so the runtime
            # is never left in an intermediate state.
            _rollback()
            raise

        if drain_error is not None:
            _rollback()
            raise drain_error

        # 5. Drain succeeded: unload old versions.
        for old in displaced:
            old.draining = True  # already set; ensure
        if disabled_id is not None:
            return self._plugin_view(disabled_id) if disabled_id in self._plugins else {"disabled": disabled_id}
        # Return a summary of what changed.
        return {
            "reason": reason,
            "active": {pid: str(lp.version) for pid, lp in sorted(new_active.items())},
        }

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    async def invoke(
        self,
        plugin_id: str,
        capability: str,
        payload: dict,
        *,
        caller_id: str = "<host>",
    ) -> dict:
        """Invoke a capability on an enabled plugin.

        Tracks in-flight requests for draining and records failures for
        isolation.  A failure does not affect other plugins or the host.
        """
        record = self._require_record(plugin_id)
        if record.state == PluginState.ISOLATED:
            raise IsolationError(
                f"plugin {plugin_id!r} is isolated and cannot serve",
                plugin_id=plugin_id,
                reason=self._tracker(plugin_id).status().last_error,
            )
        loaded = self._active.get(plugin_id)
        if loaded is None or record.state != PluginState.ENABLED:
            raise PluginStateError(
                f"plugin {plugin_id!r} is not enabled",
                current_state=record.state.value,
                required_state=PluginState.ENABLED.value,
            )
        if not loaded.acquire():
            raise PluginStateError(
                f"plugin {plugin_id!r} is draining and not accepting new requests",
                current_state="draining",
                required_state=PluginState.ENABLED.value,
            )
        tracker = self._tracker(plugin_id)
        # The context is bound to the plugin's own id so that capability
        # calls made *by* this plugin are checked against its declared
        # ``requires`` list.  The ``caller_id`` parameter only affects the
        # audit trail for the initial invocation.
        ctx = PluginContext(self, plugin_id)
        try:
            result = await loaded.instance.handle(capability, payload, ctx)
        except asyncio.CancelledError:
            # Don't count cancellation as a plugin failure.
            raise
        except Exception as exc:
            isolated = tracker.record_failure(
                f"{type(exc).__name__}: {exc}", operation="invoke"
            )
            logger.warning(
                "plugin %s invoke error: %s", plugin_id, exc, exc_info=True
            )
            if isolated:
                await self._isolate(plugin_id, tracker)
            raise
        else:
            tracker.record_success()
            return result
        finally:
            loaded.release()

    async def _isolate(self, plugin_id: str, tracker: FailureTracker) -> None:
        """Deactivate an isolated plugin (called from the invoke path)."""
        async with self._lock:
            record = self._plugins.get(plugin_id)
            if record is None:
                return
            record.state = PluginState.ISOLATED
            record.active_version = None
            loaded = self._active.pop(plugin_id, None)
            if loaded is not None:
                loaded.draining = True
                # Best-effort drain; isolation is a safety valve, so we do
                # not block on long-running requests here.
                await loaded.wait_drained_async(timeout=self._drain_timeout)

    # ------------------------------------------------------------------
    # Capability routing (with declaration check + audit)
    # ------------------------------------------------------------------

    def _find_provider(self, capability: str) -> Optional[str]:
        providers = []
        for pid, record in self._plugins.items():
            if record.state != PluginState.ENABLED:
                continue
            loaded = self._active.get(pid)
            if loaded is not None and capability in loaded.manifest.capabilities:
                providers.append(pid)
        if not providers:
            return None
        if len(providers) > 1:
            # Deterministic: this should have been caught at resolution, but
            # guard anyway.
            providers.sort()
        return providers[0]

    async def invoke_capability(
        self, caller_id: str, capability: str, payload: dict
    ) -> dict:
        """Route a capability call from one plugin to another.

        Checks the caller's declared ``requires`` list, audits the call, and
        rejects undeclared calls with
        :class:`~plugin_runtime.errors.UndeclaredCapabilityError`.
        """
        provider_id = self._find_provider(capability)
        if provider_id is None:
            raise PluginNotFoundError(
                f"no enabled plugin provides capability {capability!r}"
            )
        # Declaration check (host is always allowed).
        if caller_id != "<host>":
            caller_record = self._plugins.get(caller_id)
            if caller_record is None:
                raise PluginNotFoundError(f"caller {caller_id!r} is not installed")
            caller_loaded = self._active.get(caller_id)
            declared = (
                caller_loaded.manifest.requires
                if caller_loaded is not None
                else []
            )
            if capability not in declared:
                reason = (
                    f"caller {caller_id!r} did not declare capability "
                    f"{capability!r} in its manifest 'requires' list"
                )
                self._audit.reject(caller_id, capability, reason)
                raise UndeclaredCapabilityError(
                    reason, caller=caller_id, capability=capability
                )
        self._audit.call(caller_id, capability, provider_id)
        return await self.invoke(provider_id, capability, payload,
                                 caller_id=caller_id)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover(self, plugin_id: str) -> dict:
        """Recover an isolated plugin: clear failures and re-enable.

        Returns the plugin view.  If the plugin is not isolated, this is a
        no-op that returns the current view.
        """
        async with self._lock:
            record = self._require_record(plugin_id)
            tracker = self._tracker(plugin_id)
            if record.state != PluginState.ISOLATED:
                return self._plugin_view(plugin_id)
            tracker.reset()
            record.state = PluginState.DISABLED
            record.active_version = None
            # Re-enable through the normal path (resolution + load).
            resolution = self._resolve_targets(extra_enable=plugin_id)
            await self._commit_resolution(resolution, reason="recover")
            return self._plugin_view(plugin_id)

    # ------------------------------------------------------------------
    # Isolation / audit queries
    # ------------------------------------------------------------------

    def isolation_status(self, plugin_id: str) -> IsolationStatus:
        self._require_record(plugin_id)
        return self._tracker(plugin_id).status()

    def audit_entries(self, *, caller: Optional[str] = None,
                      capability: Optional[str] = None,
                      kind: Optional[str] = None,
                      limit: int = 100) -> List[dict]:
        return [e.to_dict() for e in self._audit.entries(
            caller=caller, capability=capability, kind=kind, limit=limit
       )]

    def audit_rejections(self, *, caller: Optional[str] = None,
                         capability: Optional[str] = None,
                         limit: int = 100) -> List[dict]:
        return [e.to_dict() for e in self._audit.rejections(
            caller=caller, capability=capability, limit=limit
        )]
