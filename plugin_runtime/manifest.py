"""Plugin manifest: self-describing metadata every plugin must provide.

A manifest declares the plugin's identity, version, dependencies,
exposed capabilities and the external permissions it needs. Manifests
that are missing or invalid are rejected with a distinguishable reason,
and a rejected manifest never results in a partially loaded plugin.

Manifest format (JSON)::

    {
      "id": "text-search",                 // required, [a-z0-9][a-z0-9-]*
      "version": "1.2.0",                  // required, semver
      "entrypoint": "pkg.module:factory",  // optional
      "description": "...",                // optional
      "capabilities": ["search"],          // optional, exposed abilities
      "permissions": ["http.get"],         // optional, host abilities used
      "dependencies": {"tokenizer": "^1.0.0"}  // optional, id -> range
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .errors import ManifestError, ManifestErrorReason as R
from .version import Version, VersionRange

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)*$")


def _require_str(data: dict, key: str) -> str:
    if key not in data:
        raise ManifestError(R.MISSING_FIELD, f"manifest is missing required field '{key}'", key)
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ManifestError(
            R.INVALID_FIELD_TYPE, f"field '{key}' must be a non-empty string", key
        )
    return value


def _validate_name(kind: str, value: object) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ManifestError(
            R.INVALID_CAPABILITY if kind == "capability" else R.INVALID_PERMISSION,
            f"invalid {kind} name: {value!r}",
        )
    return value


@dataclass(frozen=True)
class Dependency:
    """A dependency on another plugin, constrained by a version range."""

    plugin_id: str
    range: VersionRange

    def to_dict(self) -> dict:
        return {"id": self.plugin_id, "range": str(self.range)}


@dataclass(frozen=True)
class PluginManifest:
    """Validated, immutable plugin manifest."""

    plugin_id: str
    version: Version
    capabilities: tuple[str, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    permissions: tuple[str, ...] = ()
    entrypoint: str | None = None
    description: str = ""

    @classmethod
    def from_dict(cls, data: object) -> "PluginManifest":
        """Validate a decoded JSON object; raises ManifestError."""
        if not isinstance(data, dict):
            raise ManifestError(
                R.MANIFEST_NOT_OBJECT, "manifest must be a JSON object"
            )

        plugin_id = _require_str(data, "id")
        if not _ID_RE.match(plugin_id):
            raise ManifestError(
                R.INVALID_PLUGIN_ID,
                f"plugin id {plugin_id!r} must match {_ID_RE.pattern}",
                "id",
            )

        raw_version = _require_str(data, "version")
        try:
            version = Version.parse(raw_version)
        except ValueError as exc:
            raise ManifestError(R.INVALID_VERSION, str(exc), "version") from exc

        entrypoint = data.get("entrypoint")
        if entrypoint is not None and not isinstance(entrypoint, str):
            raise ManifestError(
                R.INVALID_FIELD_TYPE, "field 'entrypoint' must be a string", "entrypoint"
            )
        description = data.get("description", "")
        if not isinstance(description, str):
            raise ManifestError(
                R.INVALID_FIELD_TYPE, "field 'description' must be a string", "description"
            )

        capabilities = data.get("capabilities", [])
        if not isinstance(capabilities, list):
            raise ManifestError(
                R.INVALID_FIELD_TYPE, "field 'capabilities' must be a list", "capabilities"
            )
        caps = tuple(dict.fromkeys(_validate_name("capability", c) for c in capabilities))

        permissions = data.get("permissions", [])
        if not isinstance(permissions, list):
            raise ManifestError(
                R.INVALID_FIELD_TYPE, "field 'permissions' must be a list", "permissions"
            )
        perms = tuple(dict.fromkeys(_validate_name("permission", p) for p in permissions))

        raw_deps = data.get("dependencies", {})
        if not isinstance(raw_deps, dict):
            raise ManifestError(
                R.INVALID_FIELD_TYPE,
                "field 'dependencies' must be an object mapping id -> range",
                "dependencies",
            )
        deps: list[Dependency] = []
        for dep_id in sorted(raw_deps):
            if not isinstance(dep_id, str) or not _ID_RE.match(dep_id):
                raise ManifestError(
                    R.INVALID_DEPENDENCY, f"invalid dependency id {dep_id!r}", "dependencies"
                )
            if dep_id == plugin_id:
                raise ManifestError(
                    R.SELF_DEPENDENCY,
                    f"plugin '{plugin_id}' cannot depend on itself",
                    "dependencies",
                )
            try:
                rng = VersionRange.parse(raw_deps[dep_id])
            except ValueError as exc:
                raise ManifestError(
                    R.INVALID_VERSION_RANGE,
                    f"dependency '{dep_id}': {exc}",
                    "dependencies",
                ) from exc
            deps.append(Dependency(dep_id, rng))

        return cls(
            plugin_id=plugin_id,
            version=version,
            capabilities=caps,
            dependencies=tuple(deps),
            permissions=perms,
            entrypoint=entrypoint,
            description=description,
        )

    @classmethod
    def from_json(cls, text: str | bytes | None) -> "PluginManifest":
        """Parse a manifest from raw JSON text; raises ManifestError."""
        if text is None or (isinstance(text, (str, bytes)) and not text.strip()):
            raise ManifestError(R.MANIFEST_MISSING, "plugin manifest is missing or empty")
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ManifestError(R.MANIFEST_NOT_JSON, f"manifest is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict:
        return {
            "id": self.plugin_id,
            "version": str(self.version),
            "entrypoint": self.entrypoint,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "dependencies": {d.plugin_id: str(d.range) for d in self.dependencies},
        }
