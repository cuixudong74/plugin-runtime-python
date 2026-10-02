# Plugin Manifest Format

Every plugin is described by a self-contained JSON manifest named
`plugin.json` located in the plugin's root directory.  The manifest declares
the plugin's identity, version, dependencies, provided capabilities, and the
external capabilities it needs to call.

## Schema

```json
{
  "id": "my-plugin",
  "version": "1.2.3",
  "description": "Optional human-readable description.",
  "dependencies": {
    "other-plugin": ">=1.0.0,<2.0.0"
  },
  "capabilities": ["payments", "refunds"],
  "requires": ["logger"],
  "entrypoint": "my_plugin:create_plugin"
}
```

| Field          | Type     | Required | Description                                                        |
|----------------|----------|----------|--------------------------------------------------------------------|
| `id`           | string   | yes      | Unique plugin identifier.  Alphanumeric, `-`, `_`, `.` allowed.   |
| `version`      | string   | yes      | SemVer version: `major.minor.patch` with optional prerelease/build. |
| `description`  | string   | no       | Human-readable description.                                        |
| `dependencies` | object   | no       | Map of `plugin-id -> version-range`.                             |
| `capabilities` | [string] | no       | Capabilities this plugin provides to others.                      |
| `requires`     | [string] | no       | Capabilities this plugin may call (must declare before calling).  |
| `entrypoint`   | string   | yes      | `"module:callable"` -- the callable returns a plugin instance.   |

## Validation

The manifest is strictly validated at install time.  Missing or malformed
fields are rejected with a **distinguishable error reason**:

| Error class                  | Reason                                              |
|------------------------------|-----------------------------------------------------|
| `ManifestMissingError`       | The manifest file does not exist.                  |
| `ManifestParseError`         | The file is not valid JSON.                        |
| `ManifestSchemaError`        | A required field is missing or has the wrong type. |
| `ManifestVersionError`       | The version string is not valid SemVer.            |
| `ManifestDependencyError`    | A dependency range is not valid.                   |

A rejected manifest is never loaded -- there is no half-installed state.

## Entrypoint

The `entrypoint` field names a Python module and a callable in the format
`"module_name:callable_name"`.  The module is loaded from the plugin's
directory (the directory containing `plugin.json`).  The callable is invoked
with no arguments and must return an object implementing the plugin protocol:

```python
class Plugin:
    async def handle(self, capability: str, payload: dict, ctx: "PluginContext") -> dict:
        ...
```

The `ctx` argument is a `PluginContext` that lets the plugin call capabilities
provided by other plugins (subject to declaration checks and auditing).

## Example

See [`examples/greeter/`](../examples/greeter/) for a minimal working plugin.
