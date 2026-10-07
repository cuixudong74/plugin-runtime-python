"""Concurrency, drain-timing and timeout behaviour of the plugin runtime.

These tests pin down the operational guarantees that matter under real
traffic:

* a lifecycle operation returns when the last in-flight request on the old
  version actually finishes, not when the drain cap expires;
* draining one plugin never blocks lifecycle operations on unrelated
  plugins;
* calls made during an upgrade/disable always land on a complete version
  or get a clear, distinguishable rejection;
* concurrent batches converge deterministically without duplicate loads;
* failure counting is exact under concurrency and quarantine is stable;
* timeouts and cancellations leave the runtime consistent and usable.
"""

import asyncio
import json
import threading
import time
from collections import Counter

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


class SlowEcho(Echo):
    """Echo whose ``slow`` capability blocks until ``release`` is set."""

    def __init__(self, tag, started, release):
        super().__init__(tag)
        self.started = started
        self.release = release

    def slow(self):
        self.started.set()
        self.release.wait(30)
        return f"{self.tag}:slow-done"


def make_runtime(objects, drain_timeout=10.0, threshold=3, load_delay=0.0):
    """objects: {(plugin_id, version): plugin_obj}. Returns (runtime, load_calls)."""
    calls = []

    def loader(m):
        calls.append((m.plugin_id, str(m.version)))
        if load_delay:
            time.sleep(load_delay)
        return objects[(m.plugin_id, str(m.version))]

    rt = PluginRuntime(
        loader=loader, failure_threshold=threshold, drain_timeout=drain_timeout
    )
    return rt, calls


def start_in_flight(rt, pid="a", capability="slow"):
    """Run one blocking request in a thread; returns (thread, result holder)."""
    holder = {}

    def target():
        holder["value"] = asyncio.run(rt.invoke(pid, capability))

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, holder


# ------------------------------------------------------- drain timing


def test_upgrade_returns_when_in_flight_finishes_not_at_cap():
    started, release = threading.Event(), threading.Event()
    objects = {
        ("a", "1.0.0"): SlowEcho("a-1.0.0", started, release),
        ("a", "2.0.0"): Echo("a-2.0.0"),
    }
    rt, _ = make_runtime(objects, drain_timeout=10.0)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))
    run(rt.install(manifest("a", "2.0.0")))

    t, holder = start_in_flight(rt)
    assert started.wait(5)

    async def main():
        async def releaser():
            await asyncio.sleep(0.5)
            release.set()

        t0 = time.monotonic()
        info, _ = await asyncio.gather(rt.upgrade("a"), releaser())
        return info, time.monotonic() - t0

    info, elapsed = run(main())
    t.join(10)
    assert holder["value"] == "a-1.0.0:slow-done"  # old request ran to completion
    assert info.version == "2.0.0"
    # returned shortly after the in-flight request ended (~0.5s),
    # nowhere near the 10s drain cap
    assert elapsed < 3.0


def test_disable_returns_when_in_flight_finishes_not_at_cap():
    started, release = threading.Event(), threading.Event()
    objects = {("a", "1.0.0"): SlowEcho("a-1.0.0", started, release)}
    rt, _ = make_runtime(objects, drain_timeout=10.0)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))

    t, holder = start_in_flight(rt)
    assert started.wait(5)

    async def main():
        async def releaser():
            await asyncio.sleep(0.5)
            release.set()

        t0 = time.monotonic()
        info, _ = await asyncio.gather(rt.disable("a"), releaser())
        return info, time.monotonic() - t0

    info, elapsed = run(main())
    t.join(10)
    assert holder["value"] == "a-1.0.0:slow-done"
    assert info.status == PluginStatus.DISABLED
    assert elapsed < 3.0


# ------------------------------------------- independence across plugins


def test_unrelated_lifecycle_ops_not_blocked_by_draining_plugin():
    started, release = threading.Event(), threading.Event()
    objects = {
        ("a", "1.0.0"): SlowEcho("a-1.0.0", started, release),
        ("a", "2.0.0"): Echo("a-2.0.0"),
        ("b", "1.0.0"): Echo("b-1.0.0"),
        ("c", "1.0.0"): Echo("c-1.0.0"),
        ("c", "2.0.0"): Echo("c-2.0.0"),
    }
    rt, _ = make_runtime(objects, drain_timeout=30.0)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.install(manifest("b", "1.0.0")))
    run(rt.install(manifest("c", "1.0.0")))
    run(rt.install(manifest("c", "2.0.0")))
    run(rt.enable("a"))  # serves 1.0.0; 2.0.0 is installed afterwards
    run(rt.enable("c"))
    run(rt.install(manifest("a", "2.0.0")))

    t, _ = start_in_flight(rt)
    assert started.wait(5)

    async def main():
        upgrade_a = asyncio.ensure_future(rt.upgrade("a"))
        await asyncio.sleep(0.2)  # 'a' committed the swap and is now draining
        t0 = time.monotonic()
        await rt.enable("b")    # unrelated enable
        await rt.upgrade("c")   # unrelated upgrade
        await rt.disable("b")   # unrelated disable
        elapsed = time.monotonic() - t0
        # 'a' is still draining (its in-flight request was never released)
        assert not upgrade_a.done()
        release.set()
        await upgrade_a
        return elapsed

    elapsed = run(main())
    t.join(10)
    # unrelated ops finished while 'a' was still draining, far below the cap
    assert elapsed < 2.0
    assert rt.get_plugin("a").version == "2.0.0"
    assert rt.get_plugin("c").version == "2.0.0"
    assert rt.get_plugin("b").status == PluginStatus.DISABLED


# --------------------------------------- calls during lifecycle changes


def test_calls_during_upgrade_always_land_on_a_complete_version():
    objects = {
        ("a", "1.0.0"): Echo("a-1.0.0"),
        ("a", "2.0.0"): Echo("a-2.0.0"),
    }
    # a slow loader widens the commit window so the hammer really races it
    rt, _ = make_runtime(objects, drain_timeout=10.0, load_delay=0.05)
    run(rt.install(manifest("a", "1.0.0", caps=("echo",))))
    run(rt.enable("a"))
    run(rt.install(manifest("a", "2.0.0", caps=("echo",))))

    results, errors = [], []
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                results.append(asyncio.run(rt.invoke("a", "echo", "q")))
            except Exception as exc:  # recorded and asserted below
                errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for th in threads:
        th.start()
    run(rt.upgrade("a"))
    stop.set()
    for th in threads:
        th.join(10)

    assert not errors  # no intermediate-state error ever leaks to callers
    assert results
    # every call was served by exactly one complete version
    assert {r.split(":")[0] for r in results} <= {"a-1.0.0", "a-2.0.0"}
    # after the upgrade only the new version serves
    assert run(rt.invoke("a", "echo", "z")) == "a-2.0.0:z"


def test_calls_during_disable_succeed_or_get_clear_rejection():
    objects = {("a", "1.0.0"): Echo("a-1.0.0")}
    rt, _ = make_runtime(objects, drain_timeout=10.0)
    run(rt.install(manifest("a", "1.0.0", caps=("echo",))))
    run(rt.enable("a"))

    results, rejections, unexpected = [], [], []
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                results.append(asyncio.run(rt.invoke("a", "echo", "q")))
            except PluginStateError as exc:
                rejections.append(str(exc))
            except Exception as exc:  # recorded and asserted below
                unexpected.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for th in threads:
        th.start()
    run(rt.disable("a"))
    stop.set()
    for th in threads:
        th.join(10)

    assert not unexpected
    assert all(r == "a-1.0.0:q" for r in results)
    # rejections are always the distinguishable "not enabled", never a
    # draining/intermediate-state error
    assert all("not enabled" in r for r in rejections)
    assert rt.get_plugin("a").status == PluginStatus.DISABLED


# -------------------------------------------- batch determinism


def test_concurrent_batch_ops_converge_without_duplicate_loads():
    objects = {
        (pid, v): Echo(f"{pid}-{v}")
        for pid in ("a", "b", "c")
        for v in ("1.0.0", "2.0.0")
    }
    rt, calls = make_runtime(objects, drain_timeout=5.0)
    run(rt.install(manifest("a", "1.0.0", caps=("echo",))))
    run(rt.enable("a"))  # a serves 1.0.0
    run(rt.install(manifest("a", "2.0.0", caps=("echo",))))
    run(rt.install(manifest("b", "1.0.0", caps=("echo",))))
    run(rt.enable("b"))
    run(rt.install(manifest("c", "1.0.0", caps=("echo",))))
    run(rt.install(manifest("c", "2.0.0", caps=("echo",))))

    async def main():
        ops = []
        for _ in range(5):
            ops += [
                rt.enable("a"),    # idempotent no-ops
                rt.upgrade("a"),   # converge to 2.0.0
                rt.disable("b"),   # idempotent after the first
                rt.enable("c"),    # converge to enabled@2.0.0
            ]
        await asyncio.gather(*ops)

    run(main())

    counts = Counter(calls)
    # the initial loads plus exactly one load per newly activated version
    assert counts[("a", "1.0.0")] == 1
    assert counts[("a", "2.0.0")] == 1
    assert counts[("b", "1.0.0")] == 1
    assert counts[("c", "2.0.0")] == 1
    assert ("c", "1.0.0") not in counts  # enable resolves to the newest

    assert rt.get_plugin("a").status == PluginStatus.ENABLED
    assert rt.get_plugin("a").version == "2.0.0"
    assert rt.get_plugin("b").status == PluginStatus.DISABLED
    assert rt.get_plugin("c").status == PluginStatus.ENABLED
    assert rt.get_plugin("c").version == "2.0.0"
    assert run(rt.invoke("a", "echo", "x")) == "a-2.0.0:x"
    assert run(rt.invoke("c", "echo", "x")) == "c-2.0.0:x"
    with pytest.raises(PluginStateError):
        run(rt.invoke("b", "echo", "x"))


# -------------------------------------------- isolation under concurrency


class AlwaysFail:
    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def echo(self):
        with self._lock:
            self.calls += 1
        raise RuntimeError("boom")


def _failing_runtime(flaky, threshold):
    rt = PluginRuntime(
        loader=lambda m: flaky, failure_threshold=threshold, drain_timeout=5.0
    )
    run(rt.install(manifest("bad", "1.0.0", caps=("echo",))))
    run(rt.enable("bad"))
    return rt


def test_concurrent_failures_are_counted_exactly():
    flaky = AlwaysFail()
    rt = _failing_runtime(flaky, threshold=1000)  # never quarantines here

    n_threads, per_thread = 8, 10

    def worker():
        for _ in range(per_thread):
            with pytest.raises(RuntimeError):
                asyncio.run(rt.invoke("bad", "echo"))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    health = rt.get_plugin("bad").health
    assert flaky.calls == n_threads * per_thread
    # no lost updates and no double counting under concurrency
    assert health["consecutive_failures"] == n_threads * per_thread
    assert health["state"] == "degraded"


def test_concurrent_failures_trip_quarantine_and_fail_fast():
    flaky = AlwaysFail()
    rt = _failing_runtime(flaky, threshold=3)

    def worker():
        for _ in range(5):
            try:
                asyncio.run(rt.invoke("bad", "echo"))
            except (RuntimeError, QuarantinedError):
                pass

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    info = rt.get_plugin("bad")
    assert info.status == PluginStatus.QUARANTINED
    # every handler execution was recorded exactly once
    assert info.health["consecutive_failures"] == flaky.calls
    assert info.health["consecutive_failures"] >= 3
    # reason and recovery are queryable
    assert info.health["quarantine_reason"]
    assert info.health["recovery"]

    # quarantined calls fail fast without touching the plugin
    before = flaky.calls
    with pytest.raises(QuarantinedError):
        run(rt.invoke("bad", "echo"))
    assert flaky.calls == before


# -------------------------------------------- timeout / cancellation


def test_timeout_during_drain_blocks_nobody_and_plugin_recovers():
    blocker, started = threading.Event(), threading.Event()
    objects = {
        ("a", "1.0.0"): SlowEcho("a-1.0.0", started, blocker),
        ("a", "2.0.0"): Echo("a-2.0.0"),
        ("b", "1.0.0"): Echo("b-1.0.0"),
    }
    rt, _ = make_runtime(objects, drain_timeout=5.0)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))
    run(rt.install(manifest("a", "2.0.0")))
    run(rt.install(manifest("b", "1.0.0")))

    t, _ = start_in_flight(rt)
    assert started.wait(5)

    with pytest.raises(PluginRuntimeError, match="timed out"):
        run(rt.upgrade("a", timeout=0.3))

    # the timed-out drain holds no lock: unrelated ops are immediate
    t0 = time.monotonic()
    run(rt.enable("b"))
    assert time.monotonic() - t0 < 2.0

    # 'a' serves exactly one complete version and is not wedged:
    # a later disable works (the new version has no in-flight requests)
    assert run(rt.invoke("a", "echo", "x")) == "a-2.0.0:x"
    run(rt.disable("a", timeout=2.0))
    assert rt.get_plugin("a").status == PluginStatus.DISABLED

    blocker.set()
    t.join(10)


def test_cancel_during_drain_leaves_runtime_usable():
    blocker, started = threading.Event(), threading.Event()
    objects = {
        ("a", "1.0.0"): SlowEcho("a-1.0.0", started, blocker),
        ("a", "2.0.0"): Echo("a-2.0.0"),
        ("b", "1.0.0"): Echo("b-1.0.0"),
    }
    rt, _ = make_runtime(objects, drain_timeout=30.0)
    run(rt.install(manifest("a", "1.0.0")))
    run(rt.enable("a"))
    run(rt.install(manifest("a", "2.0.0")))
    run(rt.install(manifest("b", "1.0.0")))

    t, _ = start_in_flight(rt)
    assert started.wait(5)

    async def main():
        task = asyncio.ensure_future(rt.upgrade("a", timeout=30.0))
        await asyncio.sleep(0.2)  # swap committed, drain in progress
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # the cancelled drain holds no lock: unrelated ops are immediate
        t0 = time.monotonic()
        await rt.enable("b")
        assert time.monotonic() - t0 < 2.0
        # the cancelled plugin is not stuck: once the in-flight request is
        # released, a fresh operation on it succeeds
        blocker.set()
        return await rt.upgrade("a", timeout=5.0)

    info = run(main())
    t.join(10)
    assert info.version == "2.0.0"
    assert rt.get_plugin("a").status == PluginStatus.ENABLED
    assert run(rt.invoke("a", "echo", "w")) == "a-2.0.0:w"
