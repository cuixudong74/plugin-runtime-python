"""Concurrency, drain-timing and timeout behaviour of the plugin runtime.

These tests pin the semantics operations rely on under real traffic:

- draining waits for in-flight requests but returns as soon as they
  finish (not when the drain timeout expires);
- one plugin draining never blocks lifecycle operations on others;
- calls during upgrade/disable land on a complete version or get a
  clear, distinguishable rejection;
- failure counting and quarantine are exact under concurrency;
- timeouts and cancellation leave a consistent, recoverable state.
"""

import asyncio
import json
import threading
import time

import pytest

from plugin_runtime.errors import (
    PluginRuntimeError,
    PluginStateError,
    QuarantinedError,
)
from plugin_runtime.runtime import PluginRuntime, PluginStatus


def run(coro):
    return asyncio.run(coro)


def manifest(pid, version, deps=None, caps=("echo", "slow")):
    return json.dumps({
        "id": pid,
        "version": version,
        "capabilities": list(caps),
        "permissions": [],
        "dependencies": deps or {},
    })


class Echo:
    def __init__(self, tag):
        self.tag = tag

    def echo(self, x=None):
        return f"{self.tag}:{x}"


class TimedSlow:
    """slow() sleeps a fixed, short time (a request that finishes soon)."""

    def __init__(self, tag, delay, started=None):
        self.tag = tag
        self.delay = delay
        self.started = started or threading.Event()

    def echo(self, x=None):
        return f"{self.tag}:{x}"

    def slow(self):
        self.started.set()
        time.sleep(self.delay)
        return f"{self.tag}:slow-done"


class BlockedSlow:
    """slow() blocks until ``release`` is set (a stuck request)."""

    def __init__(self, tag, started, release):
        self.tag = tag
        self.started = started
        self.release = release

    def echo(self, x=None):
        return f"{self.tag}:{x}"

    def slow(self):
        self.started.set()
        self.release.wait(30)
        return f"{self.tag}:slow-done"


def make_runtime(objects, drain_timeout=30.0, threshold=3):
    return PluginRuntime(
        loader=lambda m: objects[(m.plugin_id, str(m.version))],
        failure_threshold=threshold,
        drain_timeout=drain_timeout,
    )


def invoke_in_thread(rt, pid, cap, holder):
    def target():
        holder["value"] = asyncio.run(rt.invoke(pid, cap))
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t


# ------------------------------------------------------- drain timing


def test_disable_returns_when_in_flight_finishes_not_at_timeout():
    started = threading.Event()
    objects = {("a", "1.0.0"): TimedSlow("a-1.0.0", 0.5, started)}
    rt = make_runtime(objects, drain_timeout=30.0)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))

    holder = {}
    t = invoke_in_thread(rt, "a", "slow", holder)
    assert started.wait(5)

    t0 = time.monotonic()
    info = run(rt.disable("a"))
    dt = time.monotonic() - t0
    t.join(10)

    assert info.status == PluginStatus.DISABLED
    assert holder["value"] == "a-1.0.0:slow-done"
    # waited for the in-flight request (~0.5s total) but returned right
    # after it finished, nowhere near the 30s drain timeout
    assert dt < 3.0, f"disable took {dt:.2f}s; expected it to track the in-flight request"
    assert dt >= 0.2, f"disable returned after {dt:.2f}s without waiting for the request"


def test_upgrade_returns_when_in_flight_finishes_not_at_timeout():
    started = threading.Event()
    objects = {
        ("a", "1.0.0"): TimedSlow("a-1.0.0", 0.5, started),
        ("a", "2.0.0"): Echo("a-2.0.0"),
    }
    rt = make_runtime(objects, drain_timeout=30.0)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))
    run(rt.install(manifest("a", "2.0.0")))

    holder = {}
    t = invoke_in_thread(rt, "a", "slow", holder)
    assert started.wait(5)

    t0 = time.monotonic()
    info = run(rt.upgrade("a"))
    dt = time.monotonic() - t0
    t.join(10)

    assert info.version == "2.0.0"
    assert holder["value"] == "a-1.0.0:slow-done"  # old request finished on old version
    assert dt < 3.0, f"upgrade took {dt:.2f}s; expected it to track the in-flight request"
    assert run(rt.invoke("a", "echo", "q")) == "a-2.0.0:q"


# ------------------------------------------- independent plugin drains


def test_draining_plugin_does_not_block_unrelated_operations():
    started, release = threading.Event(), threading.Event()
    objects = {
        ("a", "1.0.0"): BlockedSlow("a-1.0.0", started, release),
        ("b", "1.0.0"): Echo("b-1.0.0"),
        ("b", "2.0.0"): Echo("b-2.0.0"),
        ("c", "1.0.0"): Echo("c-1.0.0"),
    }
    rt = make_runtime(objects, drain_timeout=30.0)
    for pid, v in (("a", "1.0.0"), ("b", "1.0.0"), ("b", "2.0.0"), ("c", "1.0.0")):
        run(rt.install(manifest(pid, v)))
    run(rt.enable("a"))
    run(rt.enable("b"))

    holder = {}
    t = invoke_in_thread(rt, "a", "slow", holder)
    assert started.wait(5)

    async def main():
        disable_a = asyncio.ensure_future(rt.disable("a"))
        await asyncio.sleep(0.2)  # 'a' committed and is now draining
        t0 = time.monotonic()
        await rt.enable("c")
        await rt.upgrade("b")
        await rt.disable("c")
        dt = time.monotonic() - t0
        # unrelated ops completed while 'a' is still draining
        assert dt < 5.0, f"unrelated ops blocked {dt:.2f}s behind a draining plugin"
        assert not disable_a.done()  # 'a' still waiting for its stuck request
        release.set()
        await asyncio.wait_for(disable_a, 10)

    run(main())
    t.join(10)
    assert rt.get_plugin("a").status == PluginStatus.DISABLED
    assert rt.get_plugin("b").version == "2.0.0"
    assert rt.get_plugin("c").status == PluginStatus.DISABLED
    assert holder["value"] == "a-1.0.0:slow-done"


# --------------------------------- calls during upgrade / disable


def test_invokes_during_upgrade_always_land_on_a_complete_version():
    objects = {
        ("a", "1.0.0"): Echo("a-1.0.0"),
        ("a", "2.0.0"): Echo("a-2.0.0"),
    }
    rt = make_runtime(objects)
    run(rt.install(manifest("a", "1.0.0", caps=("echo",))))
    run(rt.enable("a"))
    run(rt.install(manifest("a", "2.0.0", caps=("echo",))))

    async def main():
        upgrade = asyncio.ensure_future(rt.upgrade("a"))
        results = await asyncio.gather(*[rt.invoke("a", "echo", i) for i in range(50)])
        info = await upgrade
        return results, info

    results, info = run(main())
    # every call was served by exactly one complete version; no
    # intermediate-state error surfaced
    assert {r.split(":")[0] for r in results} <= {"a-1.0.0", "a-2.0.0"}
    assert info.version == "2.0.0"
    # after the upgrade, everything lands on the new version
    assert run(rt.invoke("a", "echo", "z")) == "a-2.0.0:z"


def test_invokes_during_disable_succeed_or_get_clear_rejection():
    started, release = threading.Event(), threading.Event()
    objects = {("a", "1.0.0"): BlockedSlow("a-1.0.0", started, release)}
    rt = make_runtime(objects)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))

    holder = {}
    invoke_in_thread(rt, "a", "slow", holder)
    assert started.wait(5)

    async def main():
        disable = asyncio.ensure_future(rt.disable("a"))
        await asyncio.sleep(0.2)  # disable committed; old version draining
        outcomes = []
        for _ in range(20):
            try:
                outcomes.append(await rt.invoke("a", "echo"))
            except PluginStateError as exc:
                outcomes.append(str(exc))
        release.set()
        await asyncio.wait_for(disable, 10)
        return outcomes

    outcomes = run(main())
    # after the disable commit, every call is rejected with a clear,
    # distinguishable reason -- never an ambiguous draining error
    assert all("not enabled" in o for o in outcomes)
    assert rt.get_plugin("a").status == PluginStatus.DISABLED
    with pytest.raises(PluginStateError, match="not enabled"):
        run(rt.invoke("a", "echo"))


# --------------------------------- failure counting under concurrency


class AlwaysFails:
    def __init__(self):
        self.executions = 0
        self._lock = threading.Lock()

    def echo(self):
        with self._lock:
            self.executions += 1
        raise RuntimeError("boom")


def test_concurrent_failures_counted_exactly():
    bad = AlwaysFails()
    rt = PluginRuntime(
        loader=lambda m: bad, failure_threshold=1000, drain_timeout=5.0
    )
    run(rt.install(manifest("bad", "1.0.0", caps=("echo",))))
    run(rt.enable("bad"))

    def call():
        with pytest.raises(RuntimeError):
            asyncio.run(rt.invoke("bad", "echo"))

    threads = [threading.Thread(target=call) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    health = rt.get_plugin("bad").health
    # no failure lost, none double-counted
    assert health["consecutive_failures"] == 30
    assert bad.executions == 30


def test_concurrent_failures_quarantine_stably_and_fail_fast():
    bad = AlwaysFails()
    rt = PluginRuntime(loader=lambda m: bad, failure_threshold=5, drain_timeout=5.0)
    run(rt.install(manifest("bad", "1.0.0", caps=("echo",))))
    run(rt.enable("bad"))

    def call():
        try:
            asyncio.run(rt.invoke("bad", "echo"))
        except (RuntimeError, QuarantinedError):
            pass

    threads = [threading.Thread(target=call) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    info = rt.get_plugin("bad")
    assert info.status == PluginStatus.QUARANTINED
    # every recorded failure corresponds to exactly one real execution
    assert info.health["consecutive_failures"] == bad.executions
    assert info.health["consecutive_failures"] >= 5
    assert info.health["quarantine_reason"]
    assert info.health["recovery"]

    # quarantined calls fail fast (no drain, no execution)
    t0 = time.monotonic()
    with pytest.raises(QuarantinedError):
        run(rt.invoke("bad", "echo"))
    assert time.monotonic() - t0 < 1.0
    before = bad.executions
    with pytest.raises(QuarantinedError):
        run(rt.invoke("bad", "echo"))
    assert bad.executions == before  # short-circuited before reaching the plugin

    # recovery path still works
    rt.recover("bad")
    assert rt.get_plugin("bad").status == PluginStatus.ENABLED


# --------------------------------- timeout / cancel consistency


def test_disable_timeout_leaves_consistent_recoverable_state():
    started, release = threading.Event(), threading.Event()
    objects = {("a", "1.0.0"): BlockedSlow("a-1.0.0", started, release)}
    rt = make_runtime(objects)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))

    holder = {}
    t = invoke_in_thread(rt, "a", "slow", holder)
    assert started.wait(5)

    with pytest.raises(PluginRuntimeError, match="timed out"):
        run(rt.disable("a", timeout=0.3))
    # the commit already happened: the plugin is cleanly disabled,
    # not stuck in an intermediate state
    assert rt.get_plugin("a").status == PluginStatus.DISABLED
    with pytest.raises(PluginStateError, match="not enabled"):
        run(rt.invoke("a", "echo"))

    release.set()
    t.join(10)
    assert holder["value"] == "a-1.0.0:slow-done"  # old request ran to completion
    # the plugin is not stuck: it can be re-enabled and serves again
    run(rt.enable("a"))
    assert run(rt.invoke("a", "echo", "x")) == "a-1.0.0:x"


def test_cancelled_disable_leaves_consistent_state():
    started, release = threading.Event(), threading.Event()
    objects = {("a", "1.0.0"): BlockedSlow("a-1.0.0", started, release)}
    rt = make_runtime(objects)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))

    holder = {}
    t = invoke_in_thread(rt, "a", "slow", holder)
    assert started.wait(5)

    async def main():
        task = asyncio.ensure_future(rt.disable("a", timeout=30.0))
        await asyncio.sleep(0.2)  # commit done, drain in progress
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(main())
    release.set()
    t.join(10)
    # cancellation during drain cannot undo the commit or wedge the plugin
    assert rt.get_plugin("a").status == PluginStatus.DISABLED
    run(rt.enable("a"))
    assert run(rt.invoke("a", "echo", "y")) == "a-1.0.0:y"


# --------------------------------- convergence under a mixed storm


def test_concurrent_duplicate_ops_converge_without_duplicate_loads():
    objects = {
        ("a", "1.0.0"): Echo("a-1.0.0"),
        ("a", "2.0.0"): Echo("a-2.0.0"),
        ("b", "1.0.0"): Echo("b-1.0.0"),
    }
    calls = []
    lock = threading.Lock()

    def loader(m):
        with lock:
            calls.append((m.plugin_id, str(m.version)))
        return objects[(m.plugin_id, str(m.version))]

    rt = PluginRuntime(loader=loader, drain_timeout=10.0)
    run(rt.install(manifest("a", "1.0.0", caps=("echo",))))
    run(rt.install(manifest("b", "1.0.0", caps=("echo",))))
    run(rt.enable("a"))
    run(rt.install(manifest("a", "2.0.0", caps=("echo",))))

    async def main():
        ops = (
            [rt.upgrade("a") for _ in range(5)]
            + [rt.enable("b") for _ in range(5)]
            + [rt.enable("a") for _ in range(5)]
        )
        return await asyncio.gather(*ops)

    results = run(main())
    # whatever the arrival order, every operation observed the same end state
    assert {r.version for r in results[:10] if r.plugin_id == "a"} == {"2.0.0"}
    assert calls.count(("a", "2.0.0")) == 1
    assert calls.count(("b", "1.0.0")) == 1
    assert rt.get_plugin("a").status == PluginStatus.ENABLED
    assert rt.get_plugin("b").status == PluginStatus.ENABLED


def test_high_load_storm_always_recovers_to_a_consistent_state():
    """Many threads hammering lifecycle ops + invokes on several plugins:
    no unexpected error types, no wedged plugin, deterministic recovery."""
    objects = {
        ("a", "1.0.0"): Echo("a-1.0.0"),
        ("a", "2.0.0"): Echo("a-2.0.0"),
        ("b", "1.0.0"): Echo("b-1.0.0"),
        ("c", "1.0.0"): Echo("c-1.0.0"),
    }
    rt = make_runtime(objects, drain_timeout=5.0)
    for pid, v in objects:
        run(rt.install(manifest(pid, v, caps=("echo",))))

    errors = []

    def worker(n):
        import random

        rng = random.Random(n)
        for _ in range(15):
            pid = rng.choice("abc")
            op = rng.choice(["enable", "disable", "upgrade", "invoke"])
            try:
                if op == "invoke":
                    asyncio.run(rt.invoke(pid, "echo", n))
                else:
                    asyncio.run(getattr(rt, op)(pid))
            except (PluginRuntimeError, PluginStateError):
                pass  # legitimate rejections (state races, resolution)
            except Exception as exc:  # anything else is a bug
                errors.append(f"{op}({pid}): {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors

    # whatever order the ops landed in, the runtime is not wedged:
    # every plugin converges to enabled and serves its newest version
    for pid in "abc":
        run(rt.enable(pid))
    assert run(rt.invoke("a", "echo", "x")) in ("a-1.0.0:x", "a-2.0.0:x")
    run(rt.upgrade("a"))
    assert run(rt.invoke("a", "echo", "x")) == "a-2.0.0:x"
    assert run(rt.invoke("b", "echo", "y")) == "b-1.0.0:y"
    assert run(rt.invoke("c", "echo", "z")) == "c-1.0.0:z"
    for pid in "abc":
        assert rt.get_plugin(pid).status == PluginStatus.ENABLED
