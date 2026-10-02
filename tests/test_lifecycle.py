"""Tests for plugin lifecycle: enable, disable, upgrade, drain, atomicity."""

import asyncio

import pytest

from plugin_runtime.errors import (
    DrainTimeoutError,
    IsolationError,
    PluginLoadError,
    PluginNotFoundError,
    PluginStateError,
    ResolutionError,
)
from plugin_runtime.manifest import load_manifest
from plugin_runtime.runtime import PluginState, PluginRuntime

from .conftest import make_manifest, write_plugin


async def _install_and_enable(runtime, root, plugin_id, version, **kw):
    path = write_plugin(root, plugin_id, version, **kw)
    manifest = load_manifest(path)
    await runtime.install(manifest)
    return await runtime.enable(plugin_id)


class TestEnableDisable:
    @pytest.mark.asyncio
    async def test_enable_flow(self, runtime, plugin_root):
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0",
                                  capabilities=["cap"])
        view = runtime.get_plugin("a")
        assert view["state"] == "enabled"
        assert view["active_version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_enable_idempotent(self, runtime, plugin_root):
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0")
        # Enabling again is a no-op.
        result = await runtime.enable("a")
        assert result["state"] == "enabled"
        assert result["active_version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_disable_flow(self, runtime, plugin_root):
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0")
        result = await runtime.disable("a")
        assert result["state"] == "disabled"
        assert result["active_version"] is None

    @pytest.mark.asyncio
    async def test_disable_not_enabled(self, runtime, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0")
        await runtime.install(load_manifest(path))
        with pytest.raises(PluginStateError):
            await runtime.disable("a")

    @pytest.mark.asyncio
    async def test_enable_not_installed(self, runtime):
        with pytest.raises(PluginNotFoundError):
            await runtime.enable("ghost")

    @pytest.mark.asyncio
    async def test_disable_required_plugin_fails(self, runtime, plugin_root):
        # a depends on b; disabling b must fail because a requires it.
        await _install_and_enable(runtime, plugin_root, "b", "1.0.0")
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0",
                                  dependencies={"b": ">=1.0.0"})
        with pytest.raises(ResolutionError):
            await runtime.disable("b")
        # b is still enabled.
        assert runtime.get_plugin("b")["state"] == "enabled"

    @pytest.mark.asyncio
    async def test_uninstall_enabled_fails(self, runtime, plugin_root):
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0")
        with pytest.raises(PluginStateError):
            await runtime.uninstall("a")

    @pytest.mark.asyncio
    async def test_uninstall_disabled(self, runtime, plugin_root):
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0")
        await runtime.disable("a")
        result = await runtime.uninstall("a")
        assert result["uninstalled"] == "a"
        with pytest.raises(PluginNotFoundError):
            runtime.get_plugin("a")


class TestUpgrade:
    @pytest.mark.asyncio
    async def test_upgrade_flow(self, runtime, plugin_root):
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0",
                                  capabilities=["cap"])
        # Write and install the new version.
        path2 = write_plugin(plugin_root, "a", "2.0.0",
                             capabilities=["cap"])
        m2 = load_manifest(path2)
        result = await runtime.upgrade("a", m2)
        assert result["active_version"] == "2.0.0"
        view = runtime.get_plugin("a")
        assert view["active_version"] == "2.0.0"
        assert "1.0.0" in view["versions"]
        assert "2.0.0" in view["versions"]

    @pytest.mark.asyncio
    async def test_upgrade_same_version_idempotent(self, runtime, plugin_root):
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0")
        path2 = write_plugin(plugin_root, "a", "1.0.0")
        m2 = load_manifest(path2)
        result = await runtime.upgrade("a", m2)
        # Same version: no change, no error.
        assert result["active_version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_upgrade_with_conflict_rejected(self, runtime, plugin_root):
        # a@1.0.0 depends on b>=1.0.0; b@1.0.0 is enabled.
        # a@2.0.0 depends on b>=2.0.0; b@1.0.0 does not satisfy.
        await _install_and_enable(runtime, plugin_root, "b", "1.0.0")
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0",
                                  dependencies={"b": ">=1.0.0"})
        path2 = write_plugin(plugin_root, "a", "2.0.0",
                             dependencies={"b": ">=2.0.0"})
        m2 = load_manifest(path2)
        with pytest.raises(ResolutionError):
            await runtime.upgrade("a", m2)
        # a is still at 1.0.0 (no half-upgrade).
        assert runtime.get_plugin("a")["active_version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_upgrade_not_installed(self, runtime, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0")
        m = load_manifest(path)
        with pytest.raises(PluginNotFoundError):
            await runtime.upgrade("a", m)

    @pytest.mark.asyncio
    async def test_upgrade_wrong_id(self, runtime, plugin_root):
        await _install_and_enable(runtime, plugin_root, "a", "1.0.0")
        path2 = write_plugin(plugin_root, "b", "2.0.0")
        m2 = load_manifest(path2)
        with pytest.raises(Exception):
            await runtime.upgrade("a", m2)


class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_waits_for_inflight(self, runtime, plugin_root):
        # Plugin that blocks in-flight requests until released.
        code = '''
import asyncio
class Plugin:
    def __init__(self):
        self.release = asyncio.Event()
    async def handle(self, capability, payload, ctx):
        await self.release.wait()
        return {"done": True}
def create_plugin():
    return Plugin()
'''
        path = write_plugin(plugin_root, "a", "1.0.0", code=code)
        await runtime.install(load_manifest(path))
        await runtime.enable("a")
        # Start a long-running request.
        loaded = runtime._active["a"]
        instance = loaded.instance
        task = asyncio.create_task(
            runtime.invoke("a", "cap", {})
        )
        # Give the request time to acquire.
        await asyncio.sleep(0.1)
        assert loaded.in_flight == 1
        # Disable should wait for the in-flight request.
        disable_task = asyncio.create_task(runtime.disable("a"))
        await asyncio.sleep(0.1)
        # Not done yet (in-flight request still running).
        assert not disable_task.done()
        # Release the in-flight request.
        instance.release.set()
        result = await task
        assert result == {"done": True}
        await disable_task
        assert runtime.get_plugin("a")["state"] == "disabled"

    @pytest.mark.asyncio
    async def test_drain_timeout_rolls_back(self, runtime, plugin_root):
        # Plugin that never finishes in-flight requests.
        code = '''
import asyncio
class Plugin:
    async def handle(self, capability, payload, ctx):
        await asyncio.sleep(999)
        return {"done": True}
def create_plugin():
    return Plugin()
'''
        path = write_plugin(plugin_root, "a", "1.0.0", code=code)
        await runtime.install(load_manifest(path))
        await runtime.enable("a")
        # Start a never-ending request.
        task = asyncio.create_task(runtime.invoke("a", "cap", {}))
        await asyncio.sleep(0.1)
        # Upgrade to a new version; drain should time out and roll back.
        path2 = write_plugin(plugin_root, "a", "2.0.0")
        m2 = load_manifest(path2)
        with pytest.raises(DrainTimeoutError):
            await runtime.upgrade("a", m2)
        # Rolled back: still serving 1.0.0.
        assert runtime.get_plugin("a")["active_version"] == "1.0.0"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestNoHalfLoaded:
    @pytest.mark.asyncio
    async def test_bad_entrypoint_rejected(self, runtime, plugin_root):
        # Manifest is valid but the entrypoint module does not exist.
        path = write_plugin(plugin_root, "a", "1.0.0",
                            entrypoint="nonexistent:create")
        # Remove the module file so loading fails.
        import os
        os.remove(os.path.join(path, "nonexistent.py")) if os.path.exists(
            os.path.join(path, "nonexistent.py")) else None
        await runtime.install(load_manifest(path))
        with pytest.raises(PluginLoadError):
            await runtime.enable("a")
        # Plugin is not enabled (no half-loaded state).
        assert runtime.get_plugin("a")["state"] == "installed"

    @pytest.mark.asyncio
    async def test_factory_raises(self, runtime, plugin_root):
        code = '''
def create_plugin():
    raise RuntimeError("boom")
'''
        path = write_plugin(plugin_root, "a", "1.0.0", code=code)
        await runtime.install(load_manifest(path))
        with pytest.raises(PluginLoadError):
            await runtime.enable("a")
        assert runtime.get_plugin("a")["state"] == "installed"

    @pytest.mark.asyncio
    async def test_conflict_rejected_before_load(self, runtime, plugin_root):
        # a requires b>=2.0.0 but only b@1.0.0 is available.
        await _install_and_enable(runtime, plugin_root, "b", "1.0.0")
        path = write_plugin(plugin_root, "a", "1.0.0",
                            dependencies={"b": ">=2.0.0"})
        await runtime.install(load_manifest(path))
        with pytest.raises(ResolutionError):
            await runtime.enable("a")
        assert runtime.get_plugin("a")["state"] == "installed"


class TestMultipleInflightDrain:
    @pytest.mark.asyncio
    async def test_drain_waits_for_all_inflight(self, runtime, plugin_root):
        # Plugin that blocks in-flight requests until released.
        code = '''
import asyncio
class Plugin:
    def __init__(self):
        self.release = asyncio.Event()
    async def handle(self, capability, payload, ctx):
        await self.release.wait()
        return {"done": True}
def create_plugin():
    return Plugin()
'''
        path = write_plugin(plugin_root, "a", "1.0.0", code=code)
        await runtime.install(load_manifest(path))
        await runtime.enable("a")
        loaded = runtime._active["a"]
        instance = loaded.instance
        # Start 3 in-flight requests.
        tasks = [asyncio.create_task(runtime.invoke("a", "cap", {}))
                 for _ in range(3)]
        await asyncio.sleep(0.1)
        assert loaded.in_flight == 3
        # Disable should wait for all 3.
        disable_task = asyncio.create_task(runtime.disable("a"))
        await asyncio.sleep(0.1)
        assert not disable_task.done()
        # Release all.
        instance.release.set()
        results = await asyncio.gather(*tasks)
        assert all(r == {"done": True} for r in results)
        await disable_task
        assert runtime.get_plugin("a")["state"] == "disabled"
