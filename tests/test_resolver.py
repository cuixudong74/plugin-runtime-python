import pytest

from plugin_runtime.errors import ResolutionError, ResolutionErrorReason as R
from plugin_runtime.manifest import PluginManifest
from plugin_runtime.resolver import resolve


def m(pid, version, deps=None):
    return PluginManifest.from_dict(
        {"id": pid, "version": version, "dependencies": deps or {}}
    )


def test_resolves_dependency_chain_in_topological_order():
    plan = resolve(
        {},
        {
            "app": [m("app", "1.0.0", {"lib": "^1.0.0"})],
            "lib": [m("lib", "1.0.0"), m("lib", "1.5.0")],
        },
    )
    assert str(plan.versions["lib"]) == "1.5.0"
    assert plan.order == ("lib", "app")


def test_resolution_is_deterministic():
    candidates = {
        "a": [m("a", "1.0.0", {"b": ">=1.0.0,<3.0.0"})],
        "b": [m("b", "1.0.0"), m("b", "2.0.0"), m("b", "2.1.0")],
    }
    results = {str(resolve({}, candidates).versions) for _ in range(5)}
    assert len(results) == 1
    # shuffled candidate has same result
    shuffled = {
        "b": [m("b", "2.1.0"), m("b", "1.0.0"), m("b", "2.0.0")],
        "a": [m("a", "1.0.0", {"b": ">=1.0.0,<3.0.0"})],
    }
    assert str(resolve({}, shuffled).versions) == results.pop()


def test_version_conflict_identifies_source():
    with pytest.raises(ResolutionError) as exc_info:
        resolve(
            {},
            {
                "a": [m("a", "1.0.0", {"b": ">=2.0.0"})],
                "b": [m("b", "1.0.0")],
            },
        )
    err = exc_info.value
    assert err.reason is R.VERSION_CONFLICT
    sources = {c["source"] for c in err.details["conflicts"]}
    assert "a@1.0.0" in sources
    assert any("b" in c["requires"] for c in err.details["conflicts"])


def test_diamond_conflict_detected():
    with pytest.raises(ResolutionError) as exc_info:
        resolve(
            {},
            {
                "top": [m("top", "1.0.0", {"l": "*", "r": "*"})],
                "l": [m("l", "1.0.0", {"base": "<2.0.0"})],
                "r": [m("r", "1.0.0", {"base": ">=2.0.0"})],
                "base": [m("base", "1.0.0"), m("base", "2.0.0")],
            },
        )
    assert exc_info.value.reason is R.VERSION_CONFLICT


def test_circular_dependency_identifies_cycle():
    with pytest.raises(ResolutionError) as exc_info:
        resolve(
            {},
            {
                "a": [m("a", "1.0.0", {"b": "*"})],
                "b": [m("b", "1.0.0", {"c": "*"})],
                "c": [m("c", "1.0.0", {"a": "*"})],
            },
        )
    err = exc_info.value
    assert err.reason is R.CIRCULAR_DEPENDENCY
    assert set(err.details["cycle"]) == {"a", "b", "c"}


def test_unknown_dependency():
    with pytest.raises(ResolutionError) as exc_info:
        resolve({}, {"a": [m("a", "1.0.0", {"ghost": "*"})]})
    assert exc_info.value.reason is R.UNKNOWN_DEPENDENCY
    assert exc_info.value.details["missing"] == "ghost"


def test_enabled_version_is_kept_when_still_valid():
    current = m("lib", "1.0.0")
    plan = resolve({"lib": current}, {"lib": [current, m("lib", "2.0.0")]})
    assert str(plan.versions["lib"]) == "1.0.0"


def test_enabled_version_dropped_when_it_conflicts():
    current = m("lib", "1.0.0")
    plan = resolve(
        {"lib": current},
        {
            "lib": [current, m("lib", "2.0.0")],
            "app": [m("app", "1.0.0", {"lib": ">=2.0.0"})],
        },
    )
    assert str(plan.versions["lib"]) == "2.0.0"
