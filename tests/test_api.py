import json

import pytest
from fastapi.testclient import TestClient

from plugin_runtime.api import create_app
from plugin_runtime.runtime import PluginRuntime


class Echo:
    def __init__(self, tag):
        self.tag = tag

    def echo(self, x=None):
        return f"{self.tag}:{x}"


def make_client():
    objects = {
        ("a", "1.0.0"): Echo("a-1.0.0"),
        ("a", "2.0.0"): Echo("a-2.0.0"),
        ("b", "1.0.0"): Echo("b-1.0.0"),
    }
    rt = PluginRuntime(loader=lambda m: objects[(m.plugin_id, str(m.version))])
    return TestClient(create_app(rt)), rt


def manifest(pid, version, deps=None):
    return json.dumps({
        "id": pid, "version": version,
        "capabilities": ["echo"], "dependencies": deps or {},
    })


def test_full_lifecycle_over_http():
    client, _ = make_client()
    r = client.post("/plugins", json={"manifest": manifest("b", "1.0.0")})
    assert r.status_code == 200
    r = client.post("/plugins", json={"manifest": manifest("a", "1.0.0", {"b": "^1.0.0"})})
    assert r.status_code == 200

    r = client.post("/plugins/a/enable")
    assert r.status_code == 200 and r.json()["status"] == "enabled"

    r = client.post("/plugins/a/invoke/echo", json={"args": ["hi"]})
    assert r.json()["result"] == "a-1.0.0:hi"

    r = client.post("/plugins", json={"manifest": manifest("a", "2.0.0", {"b": "^1.0.0"})})
    r = client.post("/plugins/a/upgrade")
    assert r.json()["version"] == "2.0.0"

    r = client.post("/plugins/a/disable")
    assert r.json()["status"] == "disabled"

    r = client.get("/plugins")
    assert {p["id"] for p in r.json()} == {"a", "b"}


def test_invalid_manifest_returns_distinguishable_reason():
    client, _ = make_client()
    r = client.post("/plugins", json={"manifest": '{"id":"x"}'})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_manifest"
    assert body["reason"] == "missing_field"
    assert body["field"] == "version"


def test_resolution_conflict_returns_409_with_source():
    client, _ = make_client()
    client.post("/plugins", json={"manifest": manifest("b", "1.0.0")})
    client.post("/plugins", json={"manifest": manifest("a", "1.0.0", {"b": ">=2.0.0"})})
    r = client.post("/plugins/a/enable")
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "version_conflict"
    assert body["details"]["conflicts"]


def test_unknown_plugin_state_error():
    client, _ = make_client()
    r = client.post("/plugins/ghost/enable")
    assert r.status_code == 409
    assert r.json()["error"] == "invalid_state"


def test_audit_endpoint():
    client, rt = make_client()
    client.post("/plugins", json={"manifest": manifest("a", "1.0.0")})
    client.post("/plugins/a/enable")
    r = client.get("/audit")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
