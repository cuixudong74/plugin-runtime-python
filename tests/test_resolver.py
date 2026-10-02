"""Tests for deterministic dependency resolution.

Covers: version conflicts (with provenance), circular dependencies,
determinism, and whole-set atomicity.
"""

import pytest

from plugin_runtime.errors import (
    CircularDependencyError,
    VersionConflictError,
)
from plugin_runtime.manifest import Manifest
from plugin_runtime.resolver import AvailablePlugin, resolve
from plugin_runtime.versions import RangeSet, Version, parse_range


def _avail(plugin_id: str, versions: list, deps: dict = None) -> AvailablePlugin:
    """Build an AvailablePlugin.

    ``deps`` maps version -> {dep_id: range_str}.  If a version has no entry,
    it has no dependencies.
    """
    deps = deps or {}
    manifests = {}
    for v in versions:
        vv = Version.parse(v)
        dd = deps.get(v, {})
        manifests[vv] = Manifest(
            id=plugin_id,
            version=vv,
            entrypoint=f"{plugin_id}:create",
            dependencies={k: parse_range(r) for k, r in dd.items()},
        )
    return AvailablePlugin(
        id=plugin_id,
        versions=[Version.parse(v) for v in versions],
        manifests=manifests,
    )


def _targets(**kwargs) -> dict:
    return {k: parse_range(v) if v else RangeSet() for k, v in kwargs.items()}


class TestBasicResolution:
    def test_single_plugin(self):
        avail = {"a": _avail("a", ["1.0.0", "2.0.0"])}
        res = resolve(avail, _targets(a=""))
        assert res.selected["a"] == Version.parse("2.0.0")  # newest first

    def test_exact_version(self):
        avail = {"a": _avail("a", ["1.0.0", "2.0.0"])}
        res = resolve(avail, _targets(a="1.0.0"))
        assert res.selected["a"] == Version.parse("1.0.0")

    def test_range(self):
        avail = {"a": _avail("a", ["1.0.0", "1.5.0", "2.0.0"])}
        res = resolve(avail, _targets(a=">=1.0.0,<2.0.0"))
        assert res.selected["a"] == Version.parse("1.5.0")

    def test_dependency_pulled_in(self):
        avail = {
            "a": _avail("a", ["1.0.0"], deps={"1.0.0": {"b": ">=1.0.0"}}),
            "b": _avail("b", ["1.0.0", "2.0.0"]),
        }
        res = resolve(avail, _targets(a=""))
        assert res.selected["a"] == Version.parse("1.0.0")
        assert res.selected["b"] == Version.parse("2.0.0")  # newest satisfying

    def test_transitive_dependency(self):
        avail = {
            "a": _avail("a", ["1.0.0"], deps={"1.0.0": {"b": ">=1.0.0"}}),
            "b": _avail("b", ["1.0.0"], deps={"1.0.0": {"c": ">=1.0.0"}}),
            "c": _avail("c", ["1.0.0"]),
        }
        res = resolve(avail, _targets(a=""))
        assert set(res.selected) == {"a", "b", "c"}

    def test_multiple_targets(self):
        avail = {
            "a": _avail("a", ["1.0.0"]),
            "b": _avail("b", ["1.0.0"]),
        }
        res = resolve(avail, _targets(a="", b=""))
        assert set(res.selected) == {"a", "b"}


class TestVersionConflicts:
    def test_no_version_satisfies(self):
        avail = {"a": _avail("a", ["1.0.0", "2.0.0"])}
        with pytest.raises(VersionConflictError) as exc:
            resolve(avail, _targets(a=">=3.0.0"))
        assert exc.value.plugin_id == "a"
        assert "3.0.0" in str(exc.value)
        assert exc.value.available  # provenance: available versions listed

    def test_conflicting_dependencies(self):
        # a requires b >=2.0.0; c requires b <2.0.0; both enabled -> conflict
        avail = {
            "a": _avail("a", ["1.0.0"], deps={"1.0.0": {"b": ">=2.0.0"}}),
            "b": _avail("b", ["1.0.0", "2.0.0"]),
            "c": _avail("c", ["1.0.0"], deps={"1.0.0": {"b": "<2.0.0"}}),
        }
        with pytest.raises(VersionConflictError) as exc:
            resolve(avail, _targets(a="", c=""))
        assert exc.value.plugin_id == "b"
        # Both requirers should be named in the error.
        assert "a" in str(exc.value)
        assert "c" in str(exc.value)

    def test_dependency_not_installed(self):
        avail = {"a": _avail("a", ["1.0.0"], deps={"1.0.0": {"b": ">=1.0.0"}})}
        with pytest.raises(VersionConflictError) as exc:
            resolve(avail, _targets(a=""))
        assert exc.value.plugin_id == "b"
        assert "not installed" in str(exc.value)

    def test_target_not_installed(self):
        avail = {"a": _avail("a", ["1.0.0"])}
        with pytest.raises(VersionConflictError) as exc:
            resolve(avail, _targets(b=""))
        assert exc.value.plugin_id == "b"

    def test_conflict_is_whole_set(self):
        # A conflict must not partially resolve.
        avail = {
            "a": _avail("a", ["1.0.0"], deps={"1.0.0": {"b": ">=2.0.0"}}),
            "b": _avail("b", ["1.0.0"]),
        }
        with pytest.raises(VersionConflictError):
            resolve(avail, _targets(a=""))


class TestCircularDependencies:
    def test_simple_cycle(self):
        avail = {
            "a": _avail("a", ["1.0.0"], deps={"1.0.0": {"b": ">=1.0.0"}}),
            "b": _avail("b", ["1.0.0"], deps={"1.0.0": {"a": ">=1.0.0"}}),
        }
        with pytest.raises(CircularDependencyError) as exc:
            resolve(avail, _targets(a=""))
        cycle = exc.value.cycle
        assert cycle[0] == cycle[-1]  # closed loop
        assert set(cycle) == {"a", "b"}

    def test_self_cycle(self):
        avail = {
            "a": _avail("a", ["1.0.0"], deps={"1.0.0": {"a": ">=1.0.0"}}),
        }
        with pytest.raises(CircularDependencyError) as exc:
            resolve(avail, _targets(a=""))
        assert exc.value.cycle == ["a", "a"]

    def test_transitive_cycle(self):
        avail = {
            "a": _avail("a", ["1.0.0"], deps={"1.0.0": {"b": ">=1.0.0"}}),
            "b": _avail("b", ["1.0.0"], deps={"1.0.0": {"c": ">=1.0.0"}}),
            "c": _avail("c", ["1.0.0"], deps={"1.0.0": {"a": ">=1.0.0"}}),
        }
        with pytest.raises(CircularDependencyError) as exc:
            resolve(avail, _targets(a=""))
        assert set(exc.value.cycle) == {"a", "b", "c"}

    def test_cycle_broken_by_version_selection(self):
        # a@1.0.0 depends on b; a@2.0.0 has no deps.  The resolver should
        # backtrack and pick a@2.0.0 to avoid the cycle.
        avail = {
            "a": _avail("a", ["1.0.0", "2.0.0"], deps={
                "1.0.0": {"b": ">=1.0.0"},
            }),
            "b": _avail("b", ["1.0.0"], deps={"1.0.0": {"a": ">=1.0.0"}}),
        }
        res = resolve(avail, _targets(a=""))
        # a@1.0.0 would cycle, so backtrack to a@2.0.0 (no deps).
        assert res.selected["a"] == Version.parse("2.0.0")
        assert "b" not in res.selected


class TestDeterminism:
    def test_same_input_same_output(self):
        avail = {
            "a": _avail("a", ["1.0.0", "2.0.0"], deps={
                "1.0.0": {"b": ">=1.0.0"},
                "2.0.0": {"b": ">=2.0.0"},
            }),
            "b": _avail("b", ["1.0.0", "2.0.0", "3.0.0"]),
        }
        r1 = resolve(avail, _targets(a=""))
        r2 = resolve(avail, _targets(a=""))
        assert r1.to_dict() == r2.to_dict()

    def test_order_independent_of_insertion(self):
        avail = {
            "a": _avail("a", ["1.0.0"], deps={"1.0.0": {"b": ">=1.0.0"}}),
            "b": _avail("b", ["1.0.0"]),
        }
        r1 = resolve(avail, _targets(a=""))
        # Rebuild in different insertion order.
        avail2 = {
            "b": _avail("b", ["1.0.0"]),
            "a": _avail("a", ["1.0.0"], deps={"1.0.0": {"b": ">=1.0.0"}}),
        }
        r2 = resolve(avail2, _targets(a=""))
        assert r1.to_dict() == r2.to_dict()

    def test_newest_version_preferred(self):
        avail = {"a": _avail("a", ["1.0.0", "1.2.0", "1.10.0"])}
        res = resolve(avail, _targets(a=""))
        assert res.selected["a"] == Version.parse("1.10.0")
