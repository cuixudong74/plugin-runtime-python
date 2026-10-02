"""FastAPI surface exposing the plugin runtime over HTTP."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .errors import (
    ManifestError,
    PermissionDeniedError,
    PluginRuntimeError,
    PluginStateError,
    QuarantinedError,
    ResolutionError,
)
from .runtime import PluginInfo, PluginRuntime


class InstallRequest(BaseModel):
    manifest: str  # raw manifest JSON


class InvokeRequest(BaseModel):
    args: list[Any] = []
    kwargs: dict[str, Any] = {}


def _info_dict(info: PluginInfo) -> dict:
    return {
        "id": info.plugin_id,
        "version": info.version,
        "status": info.status.value,
        "available_versions": list(info.available_versions),
        "health": info.health,
        "pending_requests": info.pending_requests,
    }


def create_app(runtime: PluginRuntime | None = None) -> FastAPI:
    """Build the management API around a (shared) runtime instance."""
    rt = runtime or PluginRuntime()
    app = FastAPI(title="plugin-runtime")

    @app.exception_handler(ManifestError)
    async def _manifest_error(_, exc: ManifestError):
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_manifest", "reason": exc.reason.value,
                     "field": exc.field, "detail": str(exc)},
        )

    @app.exception_handler(ResolutionError)
    async def _resolution_error(_, exc: ResolutionError):
        return JSONResponse(
            status_code=409,
            content={"error": "resolution_failed", "reason": exc.reason.value,
                     "details": exc.details, "detail": str(exc)},
        )

    @app.exception_handler(PluginStateError)
    async def _state_error(_, exc: PluginStateError):
        return JSONResponse(status_code=409,
                            content={"error": "invalid_state", "detail": str(exc)})

    @app.exception_handler(QuarantinedError)
    async def _quarantined(_, exc: QuarantinedError):
        return JSONResponse(status_code=423,
                            content={"error": "quarantined", "detail": str(exc)})

    @app.exception_handler(PermissionDeniedError)
    async def _denied(_, exc: PermissionDeniedError):
        return JSONResponse(status_code=403,
                            content={"error": "permission_denied", "detail": str(exc)})

    @app.exception_handler(PluginRuntimeError)
    async def _runtime_error(_, exc: PluginRuntimeError):
        return JSONResponse(status_code=500,
                            content={"error": "runtime_error", "detail": str(exc)})

    @app.get("/plugins")
    async def list_plugins() -> list[dict]:
        return [_info_dict(i) for i in rt.list_plugins()]

    @app.post("/plugins")
    async def install(req: InstallRequest) -> dict:
        manifest = await rt.install(req.manifest)
        return {"installed": manifest.to_dict()}

    @app.get("/plugins/{plugin_id}")
    async def get_plugin(plugin_id: str) -> dict:
        return _info_dict(rt.get_plugin(plugin_id))

    @app.post("/plugins/{plugin_id}/enable")
    async def enable(plugin_id: str, timeout: float | None = None) -> dict:
        return _info_dict(await rt.enable(plugin_id, timeout=timeout))

    @app.post("/plugins/{plugin_id}/disable")
    async def disable(plugin_id: str, timeout: float | None = None) -> dict:
        return _info_dict(await rt.disable(plugin_id, timeout=timeout))

    @app.post("/plugins/{plugin_id}/upgrade")
    async def upgrade(plugin_id: str, timeout: float | None = None) -> dict:
        return _info_dict(await rt.upgrade(plugin_id, timeout=timeout))

    @app.post("/plugins/{plugin_id}/recover")
    async def recover(plugin_id: str) -> dict:
        return _info_dict(rt.recover(plugin_id))

    @app.post("/plugins/{plugin_id}/invoke/{capability}")
    async def invoke(plugin_id: str, capability: str, req: InvokeRequest) -> dict:
        result = await rt.invoke(plugin_id, capability, *req.args, **req.kwargs)
        return {"result": result}

    @app.get("/audit")
    async def audit(plugin_id: str | None = None) -> list[dict]:
        return [
            {
                "timestamp": r.timestamp,
                "plugin_id": r.plugin_id,
                "capability": r.capability,
                "outcome": r.outcome.value,
            }
            for r in rt.audit_records(plugin_id)
        ]

    return app
