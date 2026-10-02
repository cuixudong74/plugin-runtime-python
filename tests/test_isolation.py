"""Tests for failure isolation and recovery.

A single plugin's errors must not affect other plugins or the host.
Consecutive failures reaching the threshold isolate the plugin; isolation
state, reasons and recovery must be queryable.
"""

import pytest

from plugin_runtime.errors import IsolationError
from plugin_runtime.isolation import FailureTracker

from .conftest import write_plugin


class TestFailureTracker:
    def test_consecutive_failures(self):
        t = FailureTracker("a", threshold=3)
        assert not t.is_isolated()
        t.record_failure("err1", "invoke")
        assert not t.is_isolated()
        t.record_failure("err2", "invoke")
        assert not t.is_isolated()
        assert t.record_failure("err3", "invoke") is True
        assert t.is_isolated()

    def test_success_resets_counter(self):
        t = FailureTracker("a", threshold=3)
        t.record_failure("err1", "invoke")
        t.record_failure("err2", "invoke")
        t.record_success()
        # Counter reset; need 3 more to isolate.
        assert not t.is_isolated()
        t.record_failure("err3", "invoke")
        assert not t.is_isolated()

    def test_reset_clears_isolation(self):
        t = FailureTracker("a", threshold=2)
        t.record_failure("err1", "invoke")
        t.record_failure("err2", "invoke")
        assert t.is_isolated()
        t.reset()
        assert not t.is_isolated()
        assert t.status().consecutive_failures == 0
        assert t.status().history == []

    def test_status_includes_history(self):
        t = FailureTracker("a", threshold=5)
        t.record_failure("boom", "invoke")
        status = t.status()
        assert status.last_error == "boom"
        assert status.last_failure_at is not None
        assert len(status.history) == 1
        assert status.history[0]["error"] == "boom"
        assert status.history[0]["operation"] == "invoke"

    def test_threshold_configurable(self):
        t = FailureTracker("a", threshold=1)
        t.record_failure("err", "invoke")
        assert t.is_isolated()


class TestRuntimeIsolation:
    @pytest.mark.asyncio
    async def test_plugin_isolated_after_threshold(self, runtime, plugin_root):
        # Plugin that always raises.
        code = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
        raise ValueError("always fails")
def create_plugin():
    return Plugin()
'''
        path = write_plugin(plugin_root, "a", "1.0.0", code=code)
        from plugin_runtime.manifest import load_manifest
        await runtime.install(load_manifest(path))
        await runtime.enable("a")
        # threshold is 3 (from runtime fixture).
        for _ in range(3):
            with pytest.raises(ValueError):
                await runtime.invoke("a", "cap", {})
        # Now isolated.
        status = runtime.isolation_status("a")
        assert status.isolated is True
        assert status.consecutive_failures == 3
        # Cannot invoke while isolated.
        with pytest.raises(IsolationError):
            await runtime.invoke("a", "cap", {})
        # Plugin is no longer enabled.
        assert runtime.get_plugin("a")["state"] == "isolated"

    @pytest.mark.asyncio
    async def test_isolation_does_not_affect_others(self, runtime, plugin_root):
        code_bad = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
        raise ValueError("bad plugin")
def create_plugin():
    return Plugin()
'''
        code_good = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
        return {"ok": True}
def create_plugin():
    return Plugin()
'''
        write_plugin(plugin_root, "bad", "1.0.0", code=code_bad)
        write_plugin(plugin_root, "good", "1.0.0", code=code_good)
        from plugin_runtime.manifest import load_manifest
        await runtime.install(load_manifest(plugin_root + "/bad"))
        await runtime.install(load_manifest(plugin_root + "/good"))
        await runtime.enable("bad")
        await runtime.enable("good")
        # Fail the bad plugin into isolation.
        for _ in range(3):
            with pytest.raises(ValueError):
                await runtime.invoke("bad", "cap", {})
        assert runtime.isolation_status("bad").isolated
        # Good plugin still works.
        result = await runtime.invoke("good", "cap", {})
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_recover_clears_isolation(self, runtime, plugin_root):
        code = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
        raise ValueError("fails")
def create_plugin():
    return Plugin()
'''
        path = write_plugin(plugin_root, "a", "1.0.0", code=code)
        from plugin_runtime.manifest import load_manifest
        await runtime.install(load_manifest(path))
        await runtime.enable("a")
        for _ in range(3):
            with pytest.raises(ValueError):
                await runtime.invoke("a", "cap", {})
        assert runtime.isolation_status("a").isolated
        # Recover: clears isolation and re-enables.
        result = await runtime.recover("a")
        assert result["state"] == "enabled"
        assert runtime.isolation_status("a").isolated is False

    @pytest.mark.asyncio
    async def test_recover_not_isolated_is_noop(self, runtime, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0")
        from plugin_runtime.manifest import load_manifest
        await runtime.install(load_manifest(path))
        await runtime.enable("a")
        result = await runtime.recover("a")
        assert result["state"] == "enabled"

    @pytest.mark.asyncio
    async def test_isolation_status_queryable(self, runtime, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0")
        from plugin_runtime.manifest import load_manifest
        await runtime.install(load_manifest(path))
        status = runtime.isolation_status("a")
        assert status.isolated is False
        assert status.threshold == 3
        assert status.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_load_failure_counts_toward_isolation(self, runtime, plugin_root):
        # Plugin whose factory raises.
        code = '''
def create_plugin():
    raise RuntimeError("load boom")
'''
        path = write_plugin(plugin_root, "a", "1.0.0", code=code)
        from plugin_runtime.manifest import load_manifest
        await runtime.install(load_manifest(path))
        # Enable fails; the failure is recorded.
        from plugin_runtime.errors import PluginLoadError
        with pytest.raises(PluginLoadError):
            await runtime.enable("a")
        # The tracker recorded the load failure.
        status = runtime.isolation_status("a")
        assert status.last_error is not None
        assert "load boom" in status.last_error
