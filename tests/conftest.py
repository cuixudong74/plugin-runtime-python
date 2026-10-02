"""Shared test helpers: generate plugins on disk and build manifests."""

from __future__ import annotations

import json
import os
import textwrap
from typing import Dict, List, Optional

import pytest

from plugin_runtime.manifest import Manifest
from plugin_runtime.runtime import PluginRuntime
from plugin_runtime.versions import Version


def write_plugin(
    root: str,
    plugin_id: str,
    version: str,
    *,
    capabilities: List[str] = None,
    requires: List[str] = None,
    dependencies: Dict[str, str] = None,
    entrypoint: str = None,
    code: str = None,
    description: str = "",
) -> str:
    """Write a plugin directory with plugin.json and a Python module.

    Returns the path to the plugin directory.
    """
    capabilities = capabilities or []
    requires = requires or []
    dependencies = dependencies or {}
    entrypoint = entrypoint or f"{plugin_id}:create_plugin"
    plugin_dir = os.path.join(root, plugin_id)
    os.makedirs(plugin_dir, exist_ok=True)

    manifest = {
        "id": plugin_id,
        "version": version,
        "description": description,
        "dependencies": dependencies,
        "capabilities": capabilities,
        "requires": requires,
        "entrypoint": entrypoint,
    }
    with open(os.path.join(plugin_dir, "plugin.json"), "w") as fh:
        json.dump(manifest, fh)

    module_name = entrypoint.split(":", 1)[0]
    if code is None:
        code = textwrap.dedent(f"""\
            class Plugin:
                async def handle(self, capability, payload, ctx):
                    return {{"plugin": {plugin_id!r}, "version": {version!r},
                             "capability": capability, "payload": payload}}

            def create_plugin():
                return Plugin()
        """)
    with open(os.path.join(plugin_dir, module_name + ".py"), "w") as fh:
        fh.write(code)
    return plugin_dir


def make_manifest(
    plugin_id: str,
    version: str,
    *,
    capabilities: List[str] = None,
    requires: List[str] = None,
    dependencies: Dict[str, str] = None,
    entrypoint: str = None,
) -> Manifest:
    """Build a Manifest object directly (no disk)."""
    from plugin_runtime.versions import parse_range
    caps = capabilities or []
    reqs = requires or []
    deps = {k: parse_range(v) for k, v in (dependencies or {}).items()}
    return Manifest(
        id=plugin_id,
        version=Version.parse(version),
        entrypoint=entrypoint or f"{plugin_id}:create_plugin",
        dependencies=deps,
        capabilities=caps,
        requires=reqs,
    )


@pytest.fixture
def runtime(tmp_path):
    """A fresh PluginRuntime with a short drain timeout for tests."""
    return PluginRuntime(drain_timeout=2.0, failure_threshold=3)


@pytest.fixture
def plugin_root(tmp_path):
    """A directory for writing test plugins."""
    return str(tmp_path)
