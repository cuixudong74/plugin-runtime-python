"""Dependency resolution over the set of enabled plugins.

Given the currently enabled plugins plus a set of candidate manifests,
compute one consistent version assignment for every plugin. Conflicts
and dependency cycles reject the whole operation with the conflicting
source identified. Resolution is deterministic: the same inputs always
produce the same result.

Algorithm: backtracking search over plugin ids in sorted order. Each
plugin's candidate versions are tried with the currently enabled version
first (stability), then remaining versions in descending semver order.
A complete assignment is accepted only if the dependency graph is
acyclic; the topological order (dependencies first) is part of the plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ResolutionError, ResolutionErrorReason as R
from .manifest import PluginManifest
from .version import Version


@dataclass(frozen=True)
class ResolutionPlan:
    """The result of a successful resolution.

    ``versions`` maps plugin id -> chosen version for every plugin that
    must be enabled after the operation. ``order`` is a topological
    load order (dependencies first).
    """

    versions: dict[str, Version]
    order: tuple[str, ...]


def _toposort(assignment: dict[str, PluginManifest]) -> tuple[str, ...] | None:
    """Kahn's algorithm with deterministic (sorted) tie-breaking.

    Returns the load order, or None plus the cycle is reported by the
    caller via _find_cycle.
    """
    remaining = set(assignment)
    done: set[str] = set()
    order: list[str] = []
    while remaining:
        ready = sorted(
            pid for pid in remaining
            if all(d.plugin_id in done for d in assignment[pid].dependencies)
        )
        if not ready:
            return None
        order.extend(ready)
        done.update(ready)
        remaining -= set(ready)
    return tuple(order)


def _find_cycle(assignment: dict[str, PluginManifest]) -> list[str]:
    """Return one dependency cycle as a path [a, b, ..., a]."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {pid: WHITE for pid in assignment}
    stack: list[str] = []

    def visit(pid: str) -> list[str] | None:
        color[pid] = GRAY
        stack.append(pid)
        for dep in assignment[pid].dependencies:
            nxt = dep.plugin_id
            if nxt not in assignment:
                continue
            if color[nxt] == GRAY:
                return stack[stack.index(nxt):] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[pid] = BLACK
        return None

    for pid in sorted(assignment):
        if color[pid] == WHITE:
            found = visit(pid)
            if found:
                return found
    return []


def resolve(
    enabled: dict[str, PluginManifest],
    candidates: dict[str, list[PluginManifest]],
) -> ResolutionPlan:
    """Resolve a consistent set of versions.

    ``enabled`` maps plugin id -> the manifest currently enabled.
    ``candidates`` maps plugin id -> all installable manifests for that
    id (the currently enabled version is added automatically to keep it
    eligible). Every plugin in ``candidates`` is part of the solution.

    Raises ResolutionError on version conflicts, dependency cycles or
    unknown dependencies.
    """
    # Build deterministic candidate pools: enabled version first, then
    # remaining versions in descending semver order.
    pools: dict[str, list[PluginManifest]] = {}
    for pid in sorted(set(candidates) | set(enabled)):
        by_version: dict[Version, PluginManifest] = {}
        for m in candidates.get(pid, []):
            by_version[m.version] = m
        current = enabled.get(pid)
        if current is not None:
            by_version.setdefault(current.version, current)
        if not by_version:
            raise ResolutionError(
                R.VERSION_CONFLICT, f"no installable versions for plugin '{pid}'",
                {"plugin": pid},
            )
        ordered = sorted(by_version.values(), key=lambda m: m.version, reverse=True)
        if current is not None and ordered[0] is not current:
            ordered.remove(current)
            ordered.insert(0, current)
        pools[pid] = ordered

    ids = sorted(pools)

    # Unknown dependencies are a static error: reject immediately.
    for pid in ids:
        for m in pools[pid]:
            for dep in m.dependencies:
                if dep.plugin_id not in pools:
                    raise ResolutionError(
                        R.UNKNOWN_DEPENDENCY,
                        f"plugin '{pid}' {m.version} depends on unknown plugin "
                        f"'{dep.plugin_id}'",
                        {"plugin": pid, "missing": dep.plugin_id,
                         "range": str(dep.range)},
                    )

    assignment: dict[str, PluginManifest] = {}
    conflicts: list[dict] = []
    first_cycle: list[str] | None = None

    def consistent(pid: str, m: PluginManifest) -> bool:
        # Forward: deps already assigned must satisfy this manifest.
        for dep in m.dependencies:
            other = assignment.get(dep.plugin_id)
            if other is not None and not dep.range.contains(other.version):
                conflicts.append({
                    "source": f"{pid}@{m.version}",
                    "requires": f"{dep.plugin_id} {dep.range}",
                    "actual": f"{dep.plugin_id}@{other.version}",
                })
                return False
        # Backward: assigned plugins depending on pid must accept m.
        for other_pid, other in assignment.items():
            for dep in other.dependencies:
                if dep.plugin_id == pid and not dep.range.contains(m.version):
                    conflicts.append({
                        "source": f"{other_pid}@{other.version}",
                        "requires": f"{pid} {dep.range}",
                        "actual": f"{pid}@{m.version}",
                    })
                    return False
        return True

    def search(index: int) -> dict[str, PluginManifest] | None:
        nonlocal first_cycle
        if index == len(ids):
            if _toposort(assignment) is None:
                if first_cycle is None:
                    first_cycle = _find_cycle(assignment)
                return None
            return dict(assignment)
        pid = ids[index]
        for m in pools[pid]:
            if not consistent(pid, m):
                continue
            assignment[pid] = m
            result = search(index + 1)
            if result is not None:
                return result
            del assignment[pid]
        return None

    result = search(0)
    if result is None:
        if first_cycle is not None and not conflicts:
            cycle_path = " -> ".join(first_cycle)
            raise ResolutionError(
                R.CIRCULAR_DEPENDENCY,
                f"circular dependency detected: {cycle_path}",
                {"cycle": first_cycle},
            )
        if first_cycle is not None:
            cycle_path = " -> ".join(first_cycle)
            raise ResolutionError(
                R.CIRCULAR_DEPENDENCY,
                f"circular dependency detected: {cycle_path}",
                {"cycle": first_cycle, "conflicts": conflicts[:10]},
            )
        seen = set()
        unique = []
        for c in conflicts:
            key = (c["source"], c["requires"], c["actual"])
            if key not in seen:
                seen.add(key)
                unique.append(c)
        raise ResolutionError(
            R.VERSION_CONFLICT,
            "no consistent set of plugin versions exists: "
            + "; ".join(
                f"{c['source']} requires {c['requires']} but got {c['actual']}"
                for c in unique[:5]
            ),
            {"conflicts": unique[:20]},
        )

    return ResolutionPlan(
        versions={pid: m.version for pid, m in result.items()},
        order=_toposort(result) or (),
    )
