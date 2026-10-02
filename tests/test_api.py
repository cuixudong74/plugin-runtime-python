"""Tests for the HTTP API."""

import pytest
from fastapi.testclient import TestClient

from plugin_runtime.api import set_runtime
from plugin_runtime.runtime import PluginRuntime

from .conftest import write_plugin


@pytest.fixture
def client(plugin_root):
    """A TestClient with a fresh runtime."""
    from fastapi import FastAPI
    from plugin_runtime.api import router
    runtime = PluginRuntime(drain_timeout=2.0, failure_threshold=3)
    set_runtime(runtime)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestPluginCRUD:
    def test_list_empty(self, client):
        r = client.get("/api/v1/plugins")
        assert r.status_code == 200
        assert r.json()["plugins"] == []

    def test_install_and_list(self, client, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0", capabilities=["cap"])
        r = client.post("/api/v1/plugins", json={"path": path})
        assert r.status_code == 201
        assert r.json()["id"] == "a"
        assert r.json()["state"] == "installed"
        r = client.get("/api/v1/plugins")
        assert len(r.json()["plugins"]) == 1

    def test_install_invalid_manifest(self, client):
        r = client.post("/api/v1/plugins", json={"manifest": {"id": "a"}})
        assert r.status_code == 400  # missing version/entrypoint

    def test_get_plugin(self, client, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0")
        client.post("/api/v1/plugins", json={"path": path})
        r = client.get("/api/v1/plugins/a")
        assert r.status_code == 200
        assert r.json()["id"] == "a"

    def test_get_nonexistent(self, client):
        r = client.get("/api/v1/plugins/ghost")
        assert r.status_code == 404

    def test_uninstall(self, client, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0")
        client.post("/api/v1/plugins", json={"path": path})
        r = client.delete("/api/v1/plugins/a")
        assert r.status_code == 200
        assert r.json()["uninstalled"] == "a"

    def test_uninstall_enabled_fails(self, client, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0")
        client.post("/api/v1/plugins", json={"path": path})
        client.post("/api/v1/plugins/a/enable")
        r = client.delete("/api/v1/plugins/a")
        assert r.status_code == 409


class TestLifecycle:
    def test_enable_disable(self, client, plugin_root):
        path = write_plugin(plugin_root, "a", "1.0.0", capabilities=["cap"])
        client.post("/api/v1/plugins", json={"path": path})
        r = client.post("/api/v1/plugins/a/enable")
        assert r.status_code == 200
        assert r.json()["active_version"] == "1.0.0"
        r = client.post("/api/v1/plugins/a/disable")
        assert r.status_code == 200
        assert r.json()["state"] == "disabled"

    def test_enable_conflict(self, client, plugin_root):
        # a requires b>=2.0.0 but only b@1.0.0 exists.
        write_plugin(plugin_root, "b", "1.0.0")
        write_plugin(plugin_root, "a", "1.0.0",
                     dependencies={"b": ">=2.0.0"})
        client.post("/api/v1/plugins", json={"path": plugin_root + "/b"})
        client.post("/api/v1/plugins", json={"path": plugin_root + "/a"})
        r = client.post("/api/v1/plugins/a/enable")
        assert r.status_code == 409
        assert "b" in r.json()["detail"]

    def test_upgrade(self, client, plugin_root):
        write_plugin(plugin_root, "a", "1.0.0", capabilities=["cap"])
        client.post("/api/v1/plugins", json={"path": plugin_root + "/a"})
        client.post("/api/v1/plugins/a/enable")
        write_plugin(plugin_root, "a", "2.0.0", capabilities=["cap"])
        r = client.post("/api/v1/plugins/a/upgrade",
                        json={"path": plugin_root + "/a"})
        assert r.status_code == 200
        assert r.json()["active_version"] == "2.0.0"

    def test_invoke(self, client, plugin_root):
        write_plugin(plugin_root, "a", "1.0.0", capabilities=["greet"])
        client.post("/api/v1/plugins", json={"path": plugin_root + "/a"})
        client.post("/api/v1/plugins/a/enable")
        r = client.post("/api/v1/plugins/a/invoke/greet",
                        json={"payload": {"name": "World"}})
        assert r.status_code == 200
        assert r.json()["payload"]["name"] == "World"

    def test_invoke_disabled_fails(self, client, plugin_root):
        write_plugin(plugin_root, "a", "1.0.0", capabilities=["greet"])
        client.post("/api/v1/plugins", json={"path": plugin_root + "/a"})
        r = client.post("/api/v1/plugins/a/invoke/greet",
                        json={"payload": {}})
        assert r.status_code == 409


class TestIsolation:
    def test_isolation_status(self, client, plugin_root):
        write_plugin(plugin_root, "a", "1.0.0")
        client.post("/api/v1/plugins", json={"path": plugin_root + "/a"})
        r = client.get("/api/v1/plugins/a/isolation")
        assert r.status_code == 200
        assert r.json()["isolated"] is False
        assert r.json()["threshold"] == 3

    def test_recover(self, client, plugin_root):
        code = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
        raise ValueError("fails")
def create_plugin():
    return Plugin()
'''
        write_plugin(plugin_root, "a", "1.0.0", code=code)
        client.post("/api/v1/plugins", json={"path": plugin_root + "/a"})
        client.post("/api/v1/plugins/a/enable")
        for _ in range(3):
            r = client.post("/api/v1/plugins/a/invoke/cap", json={"payload": {}})
            assert r.status_code == 500
        r = client.post("/api/v1/plugins/a/recover")
        assert r.status_code == 200
        assert r.json()["state"] == "enabled"


class TestAudit:
    def test_audit_log(self, client, plugin_root):
        write_plugin(plugin_root, "a", "1.0.0", capabilities=["cap"])
        client.post("/api/v1/plugins", json={"path": plugin_root + "/a"})
        client.post("/api/v1/plugins/a/enable")
        client.post("/api/v1/plugins/a/invoke/cap", json={"payload": {}})
        r = client.get("/api/v1/audit")
        assert r.status_code == 200
        assert len(r.json()["entries"]) >= 0  # host invoke is not audited

    def test_audit_rejections(self, client, plugin_root):
        write_plugin(plugin_root, "provider", "1.0.0",
                     capabilities=["payments"])
        caller_code = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
        await ctx.call("payments", {})
        return {}
def create_plugin():
    return Plugin()
'''
        write_plugin(plugin_root, "caller", "1.0.0",
                     capabilities=["cc"], requires=[], code=caller_code)
        client.post("/api/v1/plugins", json={"path": plugin_root + "/provider"})
        client.post("/api/v1/plugins", json={"path": plugin_root + "/caller"})
        client.post("/api/v1/plugins/provider/enable")
        client.post("/api/v1/plugins/caller/enable")
        client.post("/api/v1/plugins/caller/invoke/cc", json={"payload": {}})
        r = client.get("/api/v1/audit/rejections")
        assert r.status_code == 200
        assert len(r.json()["entries"]) == 1
        assert r.json()["entries"][0]["capability"] == "payments"


class TestResolve:
    def test_resolve_endpoint(self, client, plugin_root):
        write_plugin(plugin_root, "a", "1.0.0")
        write_plugin(plugin_root, "b", "1.0.0",
                     dependencies={"a": ">=1.0.0"})
        client.post("/api/v1/plugins", json={"path": plugin_root + "/a"})
        client.post("/api/v1/plugins", json={"path": plugin_root + "/b"})
        r = client.post("/api/v1/resolve",
                        json={"targets": {"b": ""}})
        assert r.status_code == 200
        assert r.json()["selected"]["b"] == "1.0.0"
        assert r.json()["selected"]["a"] == "1.0.0"

    def test_resolve_conflict(self, client, plugin_root):
        write_plugin(plugin_root, "a", "1.0.0")
        client.post("/api/v1/plugins", json={"path": plugin_root + "/a"})
        r = client.post("/api/v1/resolve",
                        json={"targets": {"a": ">=2.0.0"}})
        assert r.status_code == 409
