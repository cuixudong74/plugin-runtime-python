"""Hot-pluggable plugin runtime for the FastAPI host.

Public API:
    PluginRuntime   -- facade managing the plugin lifecycle
    Manifest        -- parsed & validated plugin self-description
    Resolution      -- result of version resolution
    errors          -- all runtime errors (distinguishable reasons)
"""

from .errors import (
    PluginRuntimeError,
    ManifestError,
    ManifestMissingError,
    ManifestParseError,
    ManifestSchemaError,
    ManifestVersionError,
    ManifestDependencyError,
    ResolutionError,
    VersionConflictError,
    CircularDependencyError,
    CapabilityConflictError,
    LifecycleError,
    PluginNotFoundError,
    PluginStateError,
    DrainTimeoutError,
    OperationCancelledError,
    IsolationError,
    UndeclaredCapabilityError,
    PluginLoadError,
)
from .versions import Version, Range, RangeSet
from .manifest import Manifest, parse_manifest, load_manifest
from .resolver import AvailablePlugin, Resolution, resolve
from .runtime import PluginRuntime
from .audit import AuditLog, AuditEntry
from .isolation import FailureTracker, IsolationStatus
from .loader import LoadedPlugin, PluginInstance, load_plugin
from .context import PluginContext

__all__ = [
    "PluginRuntime",
    "Manifest",
    "Version",
    "Range",
    "RangeSet",
    "AvailablePlugin",
    "Resolution",
    "resolve",
    "parse_manifest",
    "load_manifest",
    "AuditLog",
    "AuditEntry",
    "FailureTracker",
    "IsolationStatus",
    "LoadedPlugin",
    "PluginInstance",
    "load_plugin",
    "PluginContext",
    # errors
    "PluginRuntimeError",
    "ManifestError",
    "ManifestMissingError",
    "ManifestParseError",
    "ManifestSchemaError",
    "ManifestVersionError",
    "ManifestDependencyError",
    "ResolutionError",
    "VersionConflictError",
    "CircularDependencyError",
    "CapabilityConflictError",
    "LifecycleError",
    "PluginNotFoundError",
    "PluginStateError",
    "DrainTimeoutError",
    "OperationCancelledError",
    "IsolationError",
    "UndeclaredCapabilityError",
    "PluginLoadError",
]
