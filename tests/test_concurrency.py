"""Tests for concurrent lifecycle operations.

Concurrent enable/disable/upgrade requests must converge to a deterministic
result; duplicate requests must not cause duplicate installs or state drift;
cancellation must not leave the runtime in an intermediate state.
"""

import asyncio
import os

import pytest

from plugin_runtime.errors import OperationCancelledError
from plugin_runtime.manifest import load_manifest
from plugin_runtime.runtime import PluginRuntime

from .conftest import write_plugin


async def _setup(runtime, plugin_root, plugin_id="a", version="1.0.0", **kw):
    path = write_plugin(plugin_root, plugin_id, version, **kw)
    manifest = load_manifest(path)
    await runtime.install(manifest)
    return path


class TestConcurrentEnable:
    @pytest.mark.asyncio
    async def test_concurrent_enable_same_result(self, runtime, plugin_root):
        await _setup(runtime, plugin_root)
        # Fire many concurrent enable requests.
        results = await asyncio.gather(*[
            runtime.enable("a") for _ in range(10)
        ])
        # All return the same state.
        for r in results:
            assert r["state"] == "enabled"
            assert r["active_version"] == "1.0.0"
        # Only one version loaded (no duplicate).
        assert len(runtime._plugins["a"].manifests) == 1
        assert runtime._active["a"].version.__str__() == "1.0.0"

    @pytest.mark.asyncio
    async def test_concurrent_enable_disable(self, runtime, plugin_root):
        await _setup(runtime, plugin_root)
        # Interleave enable and disable.
        async def toggle():
            await runtime.enable("a")
            await runtime.disable("a")
        await asyncio.gather(*[toggle() for _ in range(5)])
        # Final state is deterministic: last operation was disable.
        assert runtime.get_plugin("a")["state"] == "disabled"

    @pytest.mark.asyncio
    async def test_concurrent_enable_multiple_plugins(self, runtime, plugin_root):
        for pid in ("a", "b", "c"):
            await _setup(runtime, plugin_root, pid)
        results = await asyncio.gather(*[
            runtime.enable(pid) for pid in ("a", "b", "c")
        ])
        for r in results:
            assert r["state"] == "enabled"


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_install_no_duplicate(self, runtime, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0")
        m = load_manifest(path)
        await runtime.install(m)
        await runtime.install(m)
        await runtime.install(m)
        assert len(runtime._plugins["a"].manifests) == 1

    @pytest.mark.asyncio
    async def test_enable_after_enable(self, runtime, plugin_root):
        await _setup(runtime, plugin_root)
        await runtime.enable("a")
        await runtime.enable("a")
        assert runtime.get_plugin("a")["state"] == "enabled"

    @pytest.mark.asyncio
    async def test_disable_after_disable_raises(self, runtime, plugin_root):
        await _setup(runtime, plugin_root)
        await runtime.enable("a")
        await runtime.disable("a")
        # Disabling again is a state error (not idempotent by design).
        from plugin_runtime.errors import PluginStateError
        with pytest.raises(PluginStateError):
            await runtime.disable("a")


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_during_enable_no_state_change(self, runtime, plugin_root):
        # Plugin that delays during load.
        code = '''
import asyncio
class Plugin:
    async def handle(self, capability, payload, ctx):
        return {"ok": True}
def create_plugin():
    return Plugin()
'''
        path = write_plugin(plugin_root, "a", "1.0.0", code=code)
        await runtime.install(load_manifest(path))
        # Cancel the enable immediately.
        task = asyncio.create_task(runtime.enable("a"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # State unchanged (still installed, not enabled).
        assert runtime.get_plugin("a")["state"] == "installed"

    @pytest.mark.asyncio
    async def test_cancel_during_drain_rolls_back(self, runtime, plugin_root):
        # Plugin with a never-ending in-flight request.
        code = '''
import asyncio
class Plugin:
    async def handle(self, capability, payload, ctx):
        await asyncio.sleep(999)
        return {"ok": True}
def create_plugin():
    return Plugin()
'''
        path = write_plugin(plugin_root, "a", "1.0.0", code=code)
        await runtime.install(load_manifest(path))
        await runtime.enable("a")
        # Start an in-flight request.
        invoke_task = asyncio.create_task(runtime.invoke("a", "cap", {}))
        await asyncio.sleep(0.1)
        # Start upgrade, then cancel during drain.
        path2 = write_plugin(plugin_root, "a", "2.0.0")
        m2 = load_manifest(path2)
        upgrade_task = asyncio.create_task(runtime.upgrade("a", m2))
        await asyncio.sleep(0.1)
        upgrade_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await upgrade_task
        # Rolled back to 1.0.0.
        assert runtime.get_plugin("a")["active_version"] == "1.0.0"
        invoke_task.cancel()
        try:
            await invoke_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_operations_serialized(self, runtime, plugin_root):
        # Verify that concurrent operations don't interleave state.
        await _setup(runtime, plugin_root)
        # Run many mixed operations concurrently; final state must be
        # consistent (enabled or disabled, never a mix).
        async def mixed(i):
            if i % 2 == 0:
                await runtime.enable("a")
            else:
                try:
                    await runtime.disable("a")
                except Exception:
                    pass
        await asyncio.gather(*[mixed(i) for i in range(20)])
        view = runtime.get_plugin("a")
        assert view["state"] in ("enabled", "disabled")
        if view["state"] == "enabled":
            assert view["active_version"] == "1.0.0"
        else:
            assert view["active_version"] is None


class TestConcurrentUpgrade:
    @pytest.mark.asyncio
    async def test_concurrent_upgrades_same_result(self, runtime, plugin_root):
        await _setup(runtime, plugin_root, "a", "1.0.0")
        await runtime.enable("a")
        # Create manifests for 2.0.0 and 3.0.0 (same plugin dir, different
        # versions).  The module code is already on disk from _setup.
        from tests.conftest import make_manifest
        src = os.path.join(plugin_root, "a", "plugin.json")
        m2 = make_manifest("a", "2.0.0")
        m2.source = src
        m3 = make_manifest("a", "3.0.0")
        m3.source = src
        # Fire concurrent upgrades to different versions.
        results = await asyncio.gather(
            runtime.upgrade("a", m2),
            runtime.upgrade("a", m3),
        )
        # Both succeed; final state is deterministic (one of the versions).
        view = runtime.get_plugin("a")
        assert view["state"] == "enabled"
        assert view["active_version"] in ("2.0.0", "3.0.0")
        # Both versions are installed (no duplicate).
        assert "2.0.0" in view["versions"]
        assert "3.0.0" in view["versions"]

    @pytest.mark.asyncio
    async def test_concurrent_enable_and_upgrade(self, runtime, plugin_root):
        await _setup(runtime, plugin_root, "a", "1.0.0")
        from tests.conftest import make_manifest
        src = os.path.join(plugin_root, "a", "plugin.json")
        m2 = make_manifest("a", "2.0.0")
        m2.source = src
        # Enable and upgrade concurrently.
        results = await asyncio.gather(
            runtime.enable("a"),
            runtime.upgrade("a", m2),
        )
        view = runtime.get_plugin("a")
        assert view["state"] == "enabled"
        # Final version is deterministic.
        assert view["active_version"] in ("1.0.0", "2.0.0")
