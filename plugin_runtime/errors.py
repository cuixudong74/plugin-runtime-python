"""Error types for the plugin runtime.

Every failure raised by the runtime carries a machine-readable ``reason``
so callers can distinguish *why* an operation was rejected.
"""

from __future__ import annotations

import enum


class ManifestErrorReason(enum.Enum):
    """Why a plugin manifest was rejected."""

    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_NOT_JSON = "manifest_not_json"
    MANIFEST_NOT_OBJECT = "manifest_not_object"
    MISSING_FIELD = "missing_field"
    INVALID_FIELD_TYPE = "invalid_field_type"
    INVALID_PLUGIN_ID = "invalid_plugin_id"
    INVALID_VERSION = "invalid_version"
    INVALID_VERSION_RANGE = "invalid_version_range"
    INVALID_DEPENDENCY = "invalid_dependency"
    INVALID_CAPABILITY = "invalid_capability"
    INVALID_PERMISSION = "invalid_permission"
    SELF_DEPENDENCY = "self_dependency"


class ResolutionErrorReason(enum.Enum):
    """Why a dependency resolution failed."""

    UNKNOWN_DEPENDENCY = "unknown_dependency"
    VERSION_CONFLICT = "version_conflict"
    CIRCULAR_DEPENDENCY = "circular_dependency"


class PluginRuntimeError(Exception):
    """Base class for all runtime errors."""


class ManifestError(PluginRuntimeError):
    """A plugin manifest is missing or invalid."""

    def __init__(self, reason: ManifestErrorReason, message: str, field: str | None = None):
        self.reason = reason
        self.field = field
        super().__init__(f"[{reason.value}] {message}")


class ResolutionError(PluginRuntimeError):
    """Dependency resolution failed; the whole operation must be rejected."""

    def __init__(self, reason: ResolutionErrorReason, message: str, details: dict | None = None):
        self.reason = reason
        self.details = details or {}
        super().__init__(f"[{reason.value}] {message}")


class PluginStateError(PluginRuntimeError):
    """The requested operation is not valid for the plugin's current state."""


class PermissionDeniedError(PluginRuntimeError):
    """A plugin invoked a capability it did not declare."""

    def __init__(self, plugin_id: str, capability: str):
        self.plugin_id = plugin_id
        self.capability = capability
        super().__init__(
            f"plugin '{plugin_id}' invoked undeclared capability '{capability}'"
        )


class QuarantinedError(PluginRuntimeError):
    """The plugin is quarantined after repeated failures."""

    def __init__(self, plugin_id: str, reason: str):
        self.plugin_id = plugin_id
        self.reason = reason
        super().__init__(f"plugin '{plugin_id}' is quarantined: {reason}")
