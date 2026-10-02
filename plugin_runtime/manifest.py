"""Plugin manifest parsing and validation.

A manifest is a JSON file (``plugin.json``) that describes a plugin:

    {
      "id": "my-plugin",
      "version": "1.2.3",
      "description": "optional human-readable description",
      "dependencies": {
        "other-plugin": ">=1.0.0,<2.0.0"
      },
      "capabilities": ["payments", "refunds"],   # provided capabilities
      "requires": ["logger"],                    # capabilities called
      "entrypoint": "my_plugin:create_plugin"
    }

Validation is strict: missing or malformed fields raise a specific
:class:`~plugin_runtime.errors.ManifestError` subclass.  A plugin whose
manifest is rejected is never loaded -- no half-installed state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import (
    ManifestDependencyError,
    ManifestMissingError,
    ManifestParseError,
    ManifestSchemaError,
)
from .versions import RangeSet, Version, parse_range

MANIFEST_FILENAME = "plugin.json"


@dataclass
class Manifest:
    id: str
    version: Version
    entrypoint: str
    description: str = ""
    dependencies: Dict[str, RangeSet] = field(default_factory=dict)
    capabilities: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    source: Optional[str] = None  # path the manifest was loaded from

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version": str(self.version),
            "description": self.description,
            "dependencies": {k: str(v) for k, v in self.dependencies.items()},
            "capabilities": list(self.capabilities),
            "requires": list(self.requires),
            "entrypoint": self.entrypoint,
        }


def _require_str(obj: dict, key: str, *, required: bool = True) -> Optional[str]:
    if key not in obj:
        if required:
            raise ManifestSchemaError(
                f"manifest is missing required field {key!r}", field=key
            )
        return None
    val = obj[key]
    if not isinstance(val, str):
        raise ManifestSchemaError(
            f"field {key!r} must be a string, got {type(val).__name__}",
            field=key,
        )
    return val


def _require_str_list(obj: dict, key: str) -> List[str]:
    if key not in obj:
        return []
    val = obj[key]
    if not isinstance(val, list):
        raise ManifestSchemaError(
            f"field {key!r} must be a list of strings, got {type(val).__name__}",
            field=key,
        )
    for i, item in enumerate(val):
        if not isinstance(item, str) or not item:
            raise ManifestSchemaError(
                f"field {key!r}[{i}] must be a non-empty string", field=key
            )
    return list(val)


def _validate_id(plugin_id: str) -> None:
    if not plugin_id:
        raise ManifestSchemaError("field 'id' must not be empty", field="id")
    if not all(c.isalnum() or c in "-_." for c in plugin_id):
        raise ManifestSchemaError(
            f"plugin id {plugin_id!r} contains invalid characters "
            f"(allowed: alphanumeric, '-', '_', '.')",
            field="id",
        )


def parse_manifest(data: dict, *, source: Optional[str] = None) -> Manifest:
    """Validate a parsed JSON object and return a :class:`Manifest`.

    Raises a :class:`ManifestError` subclass on any schema violation.
    """
    if not isinstance(data, dict):
        raise ManifestSchemaError(
            f"manifest must be a JSON object, got {type(data).__name__}"
        )

    plugin_id = _require_str(data, "id")
    _validate_id(plugin_id)

    version_text = _require_str(data, "version")
    try:
        version = Version.parse(version_text)
    except Exception as exc:
        raise ManifestSchemaError(
            f"field 'version' is invalid: {exc}", field="version"
        ) from exc

    entrypoint = _require_str(data, "entrypoint")
    if not entrypoint:
        raise ManifestSchemaError(
            "field 'entrypoint' must not be empty", field="entrypoint"
        )

    description = _require_str(data, "description", required=False) or ""

    # dependencies: { "<id>": "<range>" }
    deps_raw = data.get("dependencies", {})
    if not isinstance(deps_raw, dict):
        raise ManifestSchemaError(
            f"field 'dependencies' must be an object, got {type(deps_raw).__name__}",
            field="dependencies",
        )
    dependencies: Dict[str, RangeSet] = {}
    for dep_id, dep_range in deps_raw.items():
        if not isinstance(dep_id, str) or not dep_id:
            raise ManifestSchemaError(
                f"dependency id must be a non-empty string, got {dep_id!r}",
                field="dependencies",
            )
        if not isinstance(dep_range, str):
            raise ManifestSchemaError(
                f"dependency {dep_id!r} range must be a string, "
                f"got {type(dep_range).__name__}",
                field="dependencies",
            )
        try:
            dependencies[dep_id] = parse_range(dep_range)
        except Exception as exc:
            raise ManifestDependencyError(
                f"dependency {dep_id!r} has invalid version range "
                f"{dep_range!r}: {exc}",
                dependency=dep_id,
            ) from exc

    capabilities = _require_str_list(data, "capabilities")
    requires = _require_str_list(data, "requires")

    return Manifest(
        id=plugin_id,
        version=version,
        entrypoint=entrypoint,
        description=description,
        dependencies=dependencies,
        capabilities=capabilities,
        requires=requires,
        source=source,
    )


def load_manifest(path: str) -> Manifest:
    """Load and validate a manifest from a file path.

    ``path`` may point to a ``plugin.json`` file or to a directory containing
    one.  Raises :class:`ManifestMissingError` / :class:`ManifestParseError`
    / :class:`ManifestSchemaError` as appropriate -- never a half-loaded
    plugin.
    """
    if os.path.isdir(path):
        path = os.path.join(path, MANIFEST_FILENAME)
    if not os.path.isfile(path):
        raise ManifestMissingError(f"manifest not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ManifestParseError(
            f"manifest is not valid JSON: {path}: {exc}"
        ) from exc
    except OSError as exc:
        raise ManifestMissingError(f"cannot read manifest {path}: {exc}") from exc
    return parse_manifest(data, source=path)
