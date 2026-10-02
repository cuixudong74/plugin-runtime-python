import asyncio
import threading
import time

import pytest

from plugin_runtime.errors import (
    ManifestError,
    PermissionDeniedError,
    PluginRuntimeError,
    PluginStateError,
    QuarantinedError,
)
from plugin_runtime.runtime import PluginRuntime, PluginStatus


def run(coro):
    return asyncio.run(coro)


class Echo:
    def __init__(self, tag):
        self.tag = tag

    def echo(self, x=None):
        return f"{self.tag}:{x}"


def make_runtime(versions, threshold=3, drain_timeout=5.0, host_services=None):
    """versions: {plugin_id: [version, ...]}; each gets an Echo instance."""
    objects = {}
    calls = []
    for pid, vers in versions.items():
        for v in vers:
            objects[(pid, v)] = Echo(f"{pid}-{v}")

    def loader(manifest):
        calls.append((manifest.plugin_id, str(manifest.version)))
        return objects[(manifest.plugin_id, str(manifest.version))]

    rt = PluginRuntime(
        loader=loader,
        failure_threshold=threshold,
        drain_timeout=drain_timeout,
        host_services=host_services,
    )
    return rt, calls


def manifest(pid, version, deps=None, caps=("echo",), perms=()):
    import json

    return json.dumps({
        "id": pid,
        "version": version,
        "capabilities": list(caps),
        "permissions": list(perms),
        "dependencies": deps or {},
    })


# ---------------------------------------------------------------- install


def test_invalid_manifest_rejected_without_partial_state():
    rt, _ = make_runtime({})
    with pytest.raises(ManifestError):
        run(rt.install('{"id":"broken"}'))
    assert rt.list_plugins() == []
    with pytest.raises(PluginStateError):
        run(rt.enable("broken"))


def test_reinstall_same_version_is_idempotent():
    rt, _ = make_runtime({"a": ["1.0.0"]})
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.install(manifest("a", "1.0.0")))
    assert rt.get_plugin("a").available_versions == ("1.0.0",)


# ---------------------------------------------------------------- lifecycle


def test_enable_resolves_and_starts_dependencies():
    rt, calls = make_runtime({"a": ["1.0.0"], "b": ["1.0.0"]})
    run(rt.install(manifest("b", "1.0.0")))
    run(rt.install(manifest("a", "1.0.0", deps={"b": "^1.0.0"})))
    info = run(rt.enable("a"))
    assert info.status == PluginStatus.ENABLED
    assert rt.get_plugin("b").status == PluginStatus.ENABLED
    assert run(rt.invoke("a", "echo", "x")) == "a-1.0.0:x"


def test_enable_conflict_rejects_whole_operation():
    rt, _ = make_runtime({"a": ["1.0.0"], "b": ["1.0.0"]})
    run(rt.install(manifest("b", "1.0.0")))
    run(rt.install(manifest("a", "1.0.0", deps={"b": ">=2.0.0"})))
    from plugin_runtime.errors import ResolutionError, ResolutionErrorReason

    with pytest.raises(ResolutionError) as exc_info:
        run(rt.enable("a"))
    assert exc_info.value.reason is ResolutionErrorReason.VERSION_CONFLICT
    assert rt.get_plugin("a").status == PluginStatus.INSTALLED
    assert rt.get_plugin("b").status == PluginStatus.INSTALLED


def test_failed_dependency_load_leaves_nothing_half_loaded():
    objects = {"b": Echo("b-1.0.0")}

    def loader(m):
        if m.plugin_id == "a":
            raise RuntimeError("cannot load a")
        return objects[m.plugin_id]

    rt = PluginRuntime(loader=loader, failure_threshold=10)
    run(rt.install(manifest("b", "1.0.0")))
    run(rt.install(manifest("a", "1.0.0", deps={"b": "*"})))
    with pytest.raises(PluginRuntimeError):
        run(rt.enable("a"))
    # 'a' failed to load: nothing of 'a' may remain enabled
    assert rt.get_plugin("a").status != PluginStatus.ENABLED


def test_disable_removes_dependents():
    rt, _ = make_runtime({"a": ["1.0.0"], "b": ["1.0.0"]})
    run(rt.install(manifest("b", "1.0.0")))
    run(rt.install(manifest("a", "1.0.0", deps={"b": "*"})))
    run(rt.enable("a"))
    run(rt.disable("b"))
    assert rt.get_plugin("a").status == PluginStatus.DISABLED
    assert rt.get_plugin("b").status == PluginStatus.DISABLED
    with pytest.raises(PluginStateError):
        run(rt.invoke("a", "echo"))


def test_enable_disable_idempotent():
    rt, calls = make_runtime({"a": ["1.0.0"]})
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))
    run(rt.enable("a"))
    run(rt.disable("a"))
    run(rt.disable("a"))
    assert calls.count(("a", "1.0.0")) == 1  # loaded exactly once


# ---------------------------------------------------------------- concurrency


def test_concurrent_enables_converge_to_single_load():
    rt, calls = make_runtime({"a": ["1.0.0"], "b": ["1.0.0"]})
    run(rt.install(manifest("b", "1.0.0")))
    run(rt.install(manifest("a", "1.0.0", deps={"b": "*"})))

    async def main():
        results = await asyncio.gather(*[rt.enable("a") for _ in range(10)])
        return results

    results = run(main())
    assert {r.version for r in results} == {"1.0.0"}
    assert calls.count(("a", "1.0.0")) == 1
    assert calls.count(("b", "1.0.0")) == 1


def test_concurrent_enable_disable_converge_without_drift():
    rt, _ = make_runtime({"a": ["1.0.0"]})
    run(rt.install(manifest("a", "1.0.0")))

    async def main():
        ops = [rt.enable("a"), rt.disable("a")] * 5
        await asyncio.gather(*ops)

    run(main())
    status = rt.get_plugin("a").status
    assert status in (PluginStatus.ENABLED, PluginStatus.DISABLED)
    # a second identical run reaches the same kind of steady state
    run(rt.enable("a"))
    assert rt.get_plugin("a").status == PluginStatus.ENABLED


def test_concurrent_upgrades_load_new_version_once():
    rt, calls = make_runtime({"a": ["1.0.0", "2.0.0"]})
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))
    run(rt.install(manifest("a", "2.0.0")))

    async def main():
        return await asyncio.gather(*[rt.upgrade("a") for _ in range(5)])

    results = run(main())
    assert {r.version for r in results} == {"2.0.0"}
    assert calls.count(("a", "2.0.0")) == 1


# ---------------------------------------------------------------- upgrade & drain


class SlowEcho:
    def __init__(self, tag, started, release):
        self.tag = tag
        self.started = started
        self.release = release

    def echo(self, x=None):
        return f"{self.tag}:{x}"

    def slow(self):
        self.started.set()
        self.release.wait(10)
        return f"{self.tag}:slow-done"


def test_upgrade_drains_old_version_before_unload():
    started, release = threading.Event(), threading.Event()
    old = SlowEcho("a-1.0.0", started, release)
    objects = {("a", "1.0.0"): old, ("a", "2.0.0"): Echo("a-2.0.0")}
    rt = PluginRuntime(
        loader=lambda m: objects[(m.plugin_id, str(m.version))], drain_timeout=5.0
    )
    run(rt.install(manifest("a", "1.0.0", caps=("echo", "slow"))))
    run(rt.enable("a"))

    result_holder = {}

    def in_flight():
        result_holder["value"] = asyncio.run(rt.invoke("a", "slow"))

    t = threading.Thread(target=in_flight)
    t.start()
    assert started.wait(5)
    old_inst = rt._enabled["a"]

    async def do_upgrade():
        run_install = await rt.install(manifest("a", "2.0.0"))
        return await rt.upgrade("a")

    async def observe():
        upgrade_task = asyncio.ensure_future(do_upgrade())
        # wait until the swap happened (upgrade still draining old version)
        new_serving = False
        for _ in range(500):
            if rt.get_plugin("a").version == "2.0.0":
                new_serving = True
                break
            await asyncio.sleep(0.01)
        assert new_serving, "new version never became visible"
        # during drain, new requests go to the new version only
        assert await rt.invoke("a", "echo", "q") == "a-2.0.0:q"
        # the old request is still pinned to the old (complete) version
        assert old_inst.in_flight == 1
        assert old_inst.draining
        release.set()
        info = await upgrade_task
        return info

    info = run(observe())
    t.join(10)
    assert result_holder["value"] == "a-1.0.0:slow-done"
    assert info.version == "2.0.0"
    assert rt.get_plugin("a").pending_requests == 0


def test_upgrade_timeout_leaves_consistent_state():
    blocker = threading.Event()  # never released
    old = SlowEcho("a-1.0.0", threading.Event(), blocker)
    objects = {("a", "1.0.0"): old, ("a", "2.0.0"): Echo("a-2.0.0")}
    rt = PluginRuntime(
        loader=lambda m: objects[(m.plugin_id, str(m.version))], drain_timeout=5.0
    )
    run(rt.install(manifest("a", "1.0.0", caps=("echo", "slow"))))
    run(rt.enable("a"))

    def in_flight():
        asyncio.run(rt.invoke("a", "slow"))

    t = threading.Thread(target=in_flight, daemon=True)
    t.start()
    assert old.started.wait(5)

    run(rt.install(manifest("a", "2.0.0")))
    with pytest.raises(PluginRuntimeError, match="timed out"):
        run(rt.upgrade("a", timeout=0.3))
    # state is consistent: exactly one version is exposed and it serves
    info = rt.get_plugin("a")
    assert info.version == "2.0.0"
    assert run(rt.invoke("a", "echo", "z")) == "a-2.0.0:z"


def test_cancelled_upgrade_leaves_consistent_state():
    blocker = threading.Event()
    started = threading.Event()
    old = SlowEcho("a-1.0.0", started, blocker)
    objects = {("a", "1.0.0"): old, ("a", "2.0.0"): Echo("a-2.0.0")}
    rt = PluginRuntime(
        loader=lambda m: objects[(m.plugin_id, str(m.version))], drain_timeout=30.0
    )
    run(rt.install(manifest("a", "1.0.0", caps=("echo", "slow"))))
    run(rt.enable("a"))

    def in_flight():
        asyncio.run(rt.invoke("a", "slow"))

    t = threading.Thread(target=in_flight, daemon=True)
    t.start()
    assert started.wait(5)
    run(rt.install(manifest("a", "2.0.0")))

    async def main():
        task = asyncio.ensure_future(rt.upgrade("a", timeout=30.0))
        await asyncio.sleep(0.2)  # let the swap happen, drain in progress
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(main())
    blocker.set()
    t.join(10)
    info = rt.get_plugin("a")
    assert info.status == PluginStatus.ENABLED
    assert info.version in ("1.0.0", "2.0.0")  # one complete version
    assert run(rt.invoke("a", "echo", "w")).startswith(f"a-{info.version}:")


# ---------------------------------------------------------------- isolation


class Flaky:
    def __init__(self, fail_times):
        self.remaining = fail_times

    def echo(self):
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("boom")
        return "ok"


def test_repeated_failures_quarantine_plugin():
    flaky = Flaky(fail_times=100)
    rt = PluginRuntime(
        loader=lambda m: flaky if m.plugin_id == "bad" else Echo("good-1.0.0"),
        failure_threshold=3,
    )
    run(rt.install(manifest("bad", "1.0.0")))
    run(rt.install(manifest("good", "1.0.0")))
    run(rt.enable("bad"))
    run(rt.enable("good"))

    for _ in range(3):
        with pytest.raises(RuntimeError):
            run(rt.invoke("bad", "echo"))
    # threshold reached: quarantined, further calls short-circuit
    with pytest.raises(QuarantinedError):
        run(rt.invoke("bad", "echo"))

    info = rt.get_plugin("bad")
    assert info.status == PluginStatus.QUARANTINED
    assert info.health["quarantine_reason"]
    assert info.health["recovery"]
    assert len(info.health["recent_failures"]) == 3

    # other plugins are unaffected
    assert run(rt.invoke("good", "echo")) == "good-1.0.0:None"

    # manual recovery lifts quarantine
    rt.recover("bad")
    assert rt.get_plugin("bad").status == PluginStatus.ENABLED
    flaky.remaining = 0
    assert run(rt.invoke("bad", "echo")) == "ok"


def test_load_failures_isolated_and_counted():
    def loader(m):
        if m.plugin_id == "bad":
            raise RuntimeError("load exploded")
        return Echo("good-1.0.0")

    rt = PluginRuntime(loader=loader, failure_threshold=2)
    run(rt.install(manifest("bad", "1.0.0")))
    run(rt.install(manifest("good", "1.0.0")))
    for _ in range(2):
        with pytest.raises(PluginRuntimeError):
            run(rt.enable("bad"))
    assert rt.get_plugin("bad").status == PluginStatus.QUARANTINED
    assert rt.get_plugin("bad").health["recent_failures"][0]["phase"] == "load"
    # host and other plugins still work
    run(rt.enable("good"))
    assert run(rt.invoke("good", "echo")) == "good-1.0.0:None"


# ---------------------------------------------------------------- permissions


class Spy:
    def __init__(self, ctx_holder):
        self.ctx_holder = ctx_holder

    def send(self, msg):
        return self.ctx_holder["ctx"].call("net.send", msg)

    def peek(self):
        return self.ctx_holder["ctx"].call("fs.read")


def test_undeclared_capability_call_denied_and_audited():
    ctx_holder = {}

    def loader(m):
        plugin = Spy(ctx_holder)
        return plugin

    host_calls = []

    def net_send(msg):
        host_calls.append(msg)
        return f"sent:{msg}"

    rt = PluginRuntime(loader=loader, host_services={"net.send": net_send})
    run(rt.install(manifest("spy", "1.0.0", caps=("send", "peek"), perms=("net.send",))))
    run(rt.enable("spy"))
    # give the plugin its context
    ctx_holder["ctx"] = rt._enabled["spy"].context

    assert run(rt.invoke("spy", "send", "hi")) == "sent:hi"
    with pytest.raises(PermissionDeniedError):
        run(rt.invoke("spy", "peek"))

    records = rt.audit_records("spy")
    outcomes = [(r.capability, r.outcome.value) for r in records]
    assert ("net.send", "allowed") in outcomes
    assert ("fs.read", "denied") in outcomes
