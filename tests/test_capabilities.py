"""Tests for capability declaration, audit, and rejection.

A plugin must declare a capability in its manifest's ``requires`` list
before calling it.  Undeclared calls are rejected (raised, not swallowed)
and recorded in the audit log.
"""

import pytest

from plugin_runtime.errors import UndeclaredCapabilityError
from plugin_runtime.manifest import load_manifest

from .conftest import write_plugin


class TestDeclaration:
    @pytest.mark.asyncio
    async def test_declared_call_allowed(self, runtime, plugin_root):
        # Provider plugin.
        write_plugin(plugin_root, "provider", "1.0.0",
                     capabilities=["payments"])
        # Caller plugin that declares the capability.
        caller_code = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
            result = await ctx.call("payments", {"amount": 100})
            return {"called": result}
def create_plugin():
    return Plugin()
'''
        write_plugin(plugin_root, "caller", "1.0.0",
                     capabilities=["caller-cap"],
                     requires=["payments"], code=caller_code)
        await runtime.install(load_manifest(plugin_root + "/provider"))
        await runtime.install(load_manifest(plugin_root + "/caller"))
        await runtime.enable("provider")
        await runtime.enable("caller")
        result = await runtime.invoke("caller", "caller-cap", {})
        assert "called" in result

    @pytest.mark.asyncio
    async def test_undeclared_call_rejected(self, runtime, plugin_root):
        write_plugin(plugin_root, "provider", "1.0.0",
                     capabilities=["payments"])
        # Caller does NOT declare "payments".
        caller_code = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
            result = await ctx.call("payments", {"amount": 100})
            return {"called": result}
def create_plugin():
    return Plugin()
'''
        write_plugin(plugin_root, "caller", "1.0.0",
                     capabilities=["caller-cap"],
                     requires=[], code=caller_code)
        await runtime.install(load_manifest(plugin_root + "/provider"))
        await runtime.install(load_manifest(plugin_root + "/caller"))
        await runtime.enable("provider")
        await runtime.enable("caller")
        with pytest.raises(UndeclaredCapabilityError) as exc:
            await runtime.invoke("caller", "caller-cap", {})
        assert exc.value.caller == "caller"
        assert exc.value.capability == "payments"

    @pytest.mark.asyncio
    async def test_undeclared_call_audited(self, runtime, plugin_root):
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
                     capabilities=["caller-cap"],
                     requires=[], code=caller_code)
        await runtime.install(load_manifest(plugin_root + "/provider"))
        await runtime.install(load_manifest(plugin_root + "/caller"))
        await runtime.enable("provider")
        await runtime.enable("caller")
        with pytest.raises(UndeclaredCapabilityError):
            await runtime.invoke("caller", "caller-cap", {})
        # The rejection is in the audit log.
        rejections = runtime.audit_rejections()
        assert len(rejections) == 1
        assert rejections[0]["caller"] == "caller"
        assert rejections[0]["capability"] == "payments"
        assert rejections[0]["kind"] == "reject"
        assert "not declare" in rejections[0]["reason"]

    @pytest.mark.asyncio
    async def test_allowed_call_audited(self, runtime, plugin_root):
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
                     capabilities=["caller-cap"],
                     requires=["payments"], code=caller_code)
        await runtime.install(load_manifest(plugin_root + "/provider"))
        await runtime.install(load_manifest(plugin_root + "/caller"))
        await runtime.enable("provider")
        await runtime.enable("caller")
        await runtime.invoke("caller", "caller-cap", {})
        entries = runtime.audit_entries()
        # One call entry (the caller->provider call).
        calls = [e for e in entries if e["kind"] == "call"]
        assert len(calls) == 1
        assert calls[0]["caller"] == "caller"
        assert calls[0]["capability"] == "payments"
        assert calls[0]["provider"] == "provider"

    @pytest.mark.asyncio
    async def test_no_provider_for_capability(self, runtime, plugin_root):
        caller_code = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
            await ctx.call("nonexistent", {})
            return {}
def create_plugin():
    return Plugin()
'''
        write_plugin(plugin_root, "caller", "1.0.0",
                     capabilities=["caller-cap"],
                     requires=["nonexistent"], code=caller_code)
        await runtime.install(load_manifest(plugin_root + "/caller"))
        await runtime.enable("caller")
        from plugin_runtime.errors import PluginNotFoundError
        with pytest.raises(PluginNotFoundError):
            await runtime.invoke("caller", "caller-cap", {})

    @pytest.mark.asyncio
    async def test_host_call_bypasses_declaration(self, runtime, plugin_root):
        # The host can invoke any capability without declaring it.
        write_plugin(plugin_root, "provider", "1.0.0",
                     capabilities=["payments"])
        await runtime.install(load_manifest(plugin_root + "/provider"))
        await runtime.enable("provider")
        result = await runtime.invoke("provider", "payments", {},
                                      caller_id="<host>")
        assert result is not None


class TestCapabilityConflict:
    @pytest.mark.asyncio
    async def test_duplicate_capability_providers(self, runtime, plugin_root):
        # Two plugins providing the same capability should be caught at
        # resolution time.
        write_plugin(plugin_root, "a", "1.0.0", capabilities=["payments"])
        write_plugin(plugin_root, "b", "1.0.0", capabilities=["payments"])
        await runtime.install(load_manifest(plugin_root + "/a"))
        await runtime.install(load_manifest(plugin_root + "/b"))
        await runtime.enable("a")
        # Enabling b should fail because a already provides "payments".
        from plugin_runtime.errors import CapabilityConflictError
        with pytest.raises(CapabilityConflictError):
            await runtime.enable("b")


class TestAuditFiltering:
    @pytest.mark.asyncio
    async def test_filter_by_caller(self, runtime, plugin_root):
        write_plugin(plugin_root, "provider", "1.0.0",
                     capabilities=["payments", "logger"])
        caller_code = '''
class Plugin:
    async def handle(self, capability, payload, ctx):
        if capability == "pay":
            await ctx.call("payments", {})
        elif capability == "log":
            await ctx.call("logger", {})
        return {}
def create_plugin():
    return Plugin()
'''
        write_plugin(plugin_root, "caller", "1.0.0",
                     capabilities=["cc"],
                     requires=["payments", "logger"], code=caller_code)
        await runtime.install(load_manifest(plugin_root + "/provider"))
        await runtime.install(load_manifest(plugin_root + "/caller"))
        await runtime.enable("provider")
        await runtime.enable("caller")
        await runtime.invoke("caller", "pay", {})
        await runtime.invoke("caller", "log", {})
        # Filter by caller.
        entries = runtime.audit_entries(caller="caller")
        assert len(entries) == 2
        # Filter by capability.
        entries = runtime.audit_entries(capability="payments")
        assert len(entries) == 1
        assert entries[0]["capability"] == "payments"

    @pytest.mark.asyncio
    async def test_rejections_filter(self, runtime, plugin_root):
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
        await runtime.install(load_manifest(plugin_root + "/provider"))
        await runtime.install(load_manifest(plugin_root + "/caller"))
        await runtime.enable("provider")
        await runtime.enable("caller")
        with pytest.raises(UndeclaredCapabilityError):
            await runtime.invoke("caller", "cc", {})
        rejections = runtime.audit_rejections(caller="caller")
        assert len(rejections) == 1
        assert rejections[0]["kind"] == "reject"
        # No rejections for a different caller.
        rejections = runtime.audit_rejections(caller="other")
        assert len(rejections) == 0
