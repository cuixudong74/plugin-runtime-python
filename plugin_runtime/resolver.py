"""Deterministic dependency resolution.

Given a set of *available* plugins (each with one or more versions and a
manifest per version) and a set of *target* plugins (the ones the operator
wants enabled), :func:`resolve` picks exactly one version of every plugin
such that every dependency constraint is satisfied.

Properties:

* **Whole-set atomicity** -- if no consistent selection exists, a
  :class:`~plugin_runtime.errors.ResolutionError` is raised describing the
  conflict; nothing is installed or changed.
* **Determinism** -- plugins are visited in id order, versions are tried
  newest-first, dependencies are visited in id order.  The same input always
  produces the same output.
* **Cycle rejection** -- a dependency cycle that no version selection breaks
  is rejected with the full cycle path.
* **Conflict provenance** -- a version conflict names the plugin, the
  conflicting constraints (with their requirers) and the available versions.

The implementation uses immutable state (copied dicts per recursion level),
so a failed candidate can never corrupt the in-progress selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .errors import (
    CircularDependencyError,
    ResolutionError,
    VersionConflictError,
)
from .manifest import Manifest
from .versions import RangeSet, Version


@dataclass
class AvailablePlugin:
    id: str
    versions: List[Version]  # sorted ascending
    manifests: Dict[Version, Manifest] = field(default_factory=dict)

    def sorted_versions_desc(self) -> List[Version]:
        return sorted(self.versions, reverse=True)


@dataclass
class Resolution:
    selected: Dict[str, Version]

    def get(self, plugin_id: str) -> Optional[Version]:
        return self.selected.get(plugin_id)

    def __contains__(self, plugin_id: str) -> bool:
        return plugin_id in self.selected

    def __iter__(self):
        return iter(self.selected)

    def items(self):
        return self.selected.items()

    def to_dict(self) -> Dict[str, str]:
        return {k: str(v) for k, v in sorted(self.selected.items())}


# A constraint attached to a plugin: (requirer_id, range)
Constraint = Tuple[str, RangeSet]


def resolve(
    available: Dict[str, AvailablePlugin],
    targets: Dict[str, RangeSet],
) -> Resolution:
    """Resolve a consistent set of plugin versions.

    :param available: every installed plugin, keyed by id.
    :param targets: the plugins to enable, keyed by id, with the operator's
        top-level version constraint (use an empty RangeSet for "any").
    :raises VersionConflictError: no consistent version set exists.
    :raises CircularDependencyError: a dependency cycle is unbreakable.
    """

    def _cycle_error(plugin_id: str, stack: Tuple[str, ...]) -> CircularDependencyError:
        idx = stack.index(plugin_id)
        cycle = list(stack[idx:]) + [plugin_id]
        return CircularDependencyError(
            "circular dependency detected: "
            + " -> ".join(cycle)
            + "  (no version selection breaks this cycle)",
            cycle=cycle,
        )

    def _conflict(
        plugin_id: str,
        constraints: Dict[str, List[Constraint]],
    ) -> VersionConflictError:
        cons = list(constraints.get(plugin_id, []))
        if plugin_id not in available:
            return VersionConflictError(
                f"plugin {plugin_id!r} is required but not installed; "
                f"required by: {[r for r, _ in cons]}",
                plugin_id=plugin_id,
                constraints=cons,
                available=[],
            )
        avail = available[plugin_id].sorted_versions_desc()
        detail = "; ".join(
            f"{r} requires {plugin_id} {rng}" for r, rng in cons if rng
        )
        return VersionConflictError(
            f"no version of plugin {plugin_id!r} satisfies all constraints: "
            f"{detail or 'no constraints'}; "
            f"available versions: {[str(v) for v in avail]}",
            plugin_id=plugin_id,
            constraints=cons,
            available=[str(v) for v in avail],
        )

    def _satisfies_all(
        plugin_id: str,
        version: Version,
        constraints: Dict[str, List[Constraint]],
    ) -> bool:
        return all(rng.satisfies(version) for _, rng in constraints.get(plugin_id, []))

    def _resolve(
        plugin_id: str,
        selected: Dict[str, Version],
        constraints: Dict[str, List[Constraint]],
        stack: Tuple[str, ...],
    ) -> Tuple[Dict[str, Version], Dict[str, List[Constraint]]]:
        if plugin_id in stack:
            raise _cycle_error(plugin_id, stack)
        if plugin_id in selected:
            if not _satisfies_all(plugin_id, selected[plugin_id], constraints):
                raise _conflict(plugin_id, constraints)
            return selected, constraints

        if plugin_id not in available:
            raise _conflict(plugin_id, constraints)

        last_error: Optional[ResolutionError] = None
        for version in available[plugin_id].sorted_versions_desc():
            if not _satisfies_all(plugin_id, version, constraints):
                continue
            new_selected = {**selected, plugin_id: version}
            new_constraints = {k: list(v) for k, v in constraints.items()}
            manifest = available[plugin_id].manifests.get(version)
            deps = manifest.dependencies if manifest else {}
            candidate_ok = True
            for dep_id in sorted(deps):
                new_constraints.setdefault(dep_id, []).append((plugin_id, dep_range := deps[dep_id]))
                try:
                    new_selected, new_constraints = _resolve(
                        dep_id, new_selected, new_constraints, stack + (plugin_id,)
                    )
                except ResolutionError as exc:
                    last_error = exc
                    candidate_ok = False
                    break
            if candidate_ok:
                return new_selected, new_constraints
        # No candidate worked.
        if last_error is not None:
            raise last_error
        raise _conflict(plugin_id, constraints)

    selected: Dict[str, Version] = {}
    constraints: Dict[str, List[Constraint]] = {
        pid: [("<target>", rng)] for pid, rng in targets.items()
    }
    for pid in sorted(targets):
        selected, constraints = _resolve(pid, selected, constraints, ())

    return Resolution(selected=selected)
