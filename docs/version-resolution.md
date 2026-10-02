# Version Resolution Rules

When a plugin is enabled or upgraded, the runtime resolves a **consistent set
of versions** across all enabled plugins.  This document describes the
resolution algorithm, the supported range syntax, and the error semantics.

## Version Format

Versions follow [Semantic Versioning](https://semver.org/):

```
major.minor.patch[-prerelease][+build]
```

Examples: `1.2.3`, `2.0.0-alpha.1`, `1.0.0+build.42`.

## Range Syntax

A dependency declares a version range using one or more comma-separated
constraints.  All constraints must hold for a version to satisfy the range.

| Syntax            | Meaning                              | Example              |
|-------------------|--------------------------------------|----------------------|
| `1.2.3`           | Exact version                        | `1.2.3`             |
| `>=1.0.0`         | Greater than or equal                | `>=1.0.0`           |
| `>1.0.0`          | Greater than                         | `>1.0.0`            |
| `<=2.0.0`         | Less than or equal                   | `<=2.0.0`           |
| `<2.0.0`          | Less than                            | `<2.0.0`            |
| `^1.2.0`          | Compatible with `1.2.0`              | `^1.2.0`            |
| `~1.2.3`          | Approximately `1.2.3`                | `~1.2.3`            |
| `>=1.0.0,<2.0.0`  | Compound (all must hold)             | `>=1.0.0,<2.0.0`    |

### Caret (`^`)

`^1.2.0` means `>=1.2.0,<2.0.0` -- any version that does not change the
left-most non-zero component.  For `0.x` versions, the upper bound is the next
minor version; for `0.0.x`, it is the next patch.

### Tilde (`~`)

`~1.2.3` means `>=1.2.3,<1.3.0` -- any version that does not change the minor
version.  `~1.2` (patch omitted) is equivalent to `~1.2.0`.

## Resolution Algorithm

The resolver uses **backtracking** to find a consistent version set:

1. **Targets** are the plugins the operator wants enabled.  Each target has a
   top-level constraint (usually "any version").
2. For each plugin (in id order), the resolver tries versions in
   **descending order** (newest first).
3. A candidate version is accepted only if it satisfies **all** accumulated
   constraints on that plugin (from targets and from other plugins'
   dependencies).
4. When a version is selected, its dependencies are added as constraints and
   resolved recursively.
5. If a dependency cannot be satisfied, the resolver **backtracks** and tries
   the next version.
6. If no version works, the whole resolution fails.

### Determinism

The same input always produces the same output because:

- Plugins are visited in **sorted id order**.
- Versions are tried in **descending order** (newest first).
- Dependencies are visited in **sorted id order**.

### Cycle Detection

A dependency cycle (e.g. A depends on B and B depends on A) is detected by
tracking the recursion stack.  If following dependencies leads back to a
plugin already on the stack, the cycle is reported with the full cycle path.
If a different version selection breaks the cycle, the resolver backtracks and
tries it.  If no selection breaks the cycle, the resolution is rejected.

## Error Semantics

| Error class                  | Reason                                              |
|------------------------------|-----------------------------------------------------|
| `VersionConflictError`       | No version of a plugin satisfies all constraints.  |
| `CircularDependencyError`    | A dependency cycle cannot be broken.               |
| `CapabilityConflictError`    | Two enabled plugins provide the same capability.   |

All resolution errors are **whole-set rejections**: nothing is installed,
enabled, or changed.  The error message names the conflicting plugin, the
constraints (with their requirers), and the available versions.

## Upgrade Pinning

On upgrade, the upgraded plugin is **pinned** to the new version.  If the new
version cannot be part of any consistent set, the upgrade is rejected with a
`VersionConflictError` -- the runtime stays at the old version.
