"""Distinguishable error types for the plugin runtime.

Every rejection path raises a specific error subclass so callers (and the
audit log) can tell *why* an operation was rejected without parsing strings.
"""

from __future__ import annotations


class PluginRuntimeError(Exception):
    """Base class for all plugin runtime errors."""


# ---------------------------------------------------------------------------
# Manifest errors (reject at install time, no state change)
# ---------------------------------------------------------------------------

class ManifestError(PluginRuntimeError):
    """Base class for manifest validation failures."""


class ManifestMissingError(ManifestError):
    """The manifest file does not exist."""


class ManifestParseError(ManifestError):
    """The manifest file is not valid JSON."""


class ManifestSchemaError(ManifestError):
    """The manifest JSON is missing required fields or has wrong types."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


class ManifestVersionError(ManifestError):
    """The manifest declares a version that is not valid SemVer."""


class ManifestDependencyError(ManifestError):
    """The manifest declares a dependency with an invalid version range."""

    def __init__(self, message: str, dependency: str | None = None):
        super().__init__(message)
        self.dependency = dependency


# ---------------------------------------------------------------------------
# Resolution errors (whole-set rejection, nothing installed/changed)
# ---------------------------------------------------------------------------

class ResolutionError(PluginRuntimeError):
    """Base class for dependency resolution failures."""


class VersionConflictError(ResolutionError):
    """No consistent set of versions exists; a version range cannot be met."""

    def __init__(self, message: str, plugin_id: str | None = None,
                 constraints: list | None = None, available: list | None = None):
        super().__init__(message)
        self.plugin_id = plugin_id
        self.constraints = constraints or []
        self.available = available or []


class CircularDependencyError(ResolutionError):
    """The dependency graph contains a cycle that no version selection breaks."""

    def __init__(self, message: str, cycle: list | None = None):
        super().__init__(message)
        self.cycle = cycle or []


class CapabilityConflictError(ResolutionError):
    """Two enabled plugins provide the same capability."""

    def __init__(self, message: str, capability: str | None = None,
                 providers: list | None = None):
        super().__init__(message)
        self.capability = capability
        self.providers = providers or []


# ---------------------------------------------------------------------------
# Lifecycle errors
# ---------------------------------------------------------------------------

class LifecycleError(PluginRuntimeError):
    """Base class for enable/disable/upgrade failures."""


class PluginNotFoundError(LifecycleError):
    """Referenced plugin id is not installed."""


class PluginStateError(LifecycleError):
    """Operation not valid in the plugin's current state."""

    def __init__(self, message: str, current_state: str | None = None,
                 required_state: str | None = None):
        super().__init__(message)
        self.current_state = current_state
        self.required_state = required_state


class DrainTimeoutError(LifecycleError):
    """Old version did not finish in-flight requests within the drain timeout."""

    def __init__(self, message: str, plugin_id: str | None = None,
                 in_flight: int = 0):
        super().__init__(message)
        self.plugin_id = plugin_id
        self.in_flight = in_flight


class OperationCancelledError(LifecycleError):
    """The operation was cancelled (timeout or explicit cancellation)."""

    def __init__(self, message: str, operation: str | None = None):
        super().__init__(message)
        self.operation = operation


class IsolationError(LifecycleError):
    """Plugin is isolated and cannot serve until recovered."""

    def __init__(self, message: str, plugin_id: str | None = None,
                 reason: str | None = None):
        super().__init__(message)
        self.plugin_id = plugin_id
        self.reason = reason


# ---------------------------------------------------------------------------
# Capability / invocation errors
# ---------------------------------------------------------------------------

class UndeclaredCapabilityError(PluginRuntimeError):
    """A plugin tried to call a capability it did not declare as required."""

    def __init__(self, message: str, caller: str | None = None,
                 capability: str | None = None):
        super().__init__(message)
        self.caller = caller
        self.capability = capability


class PluginLoadError(PluginRuntimeError):
    """The plugin entrypoint raised during loading/initialization."""

    def __init__(self, message: str, plugin_id: str | None = None,
                 cause: BaseException | None = None):
        super().__init__(message)
        self.plugin_id = plugin_id
        self.cause = cause
